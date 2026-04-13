import os
import json
import base64
import io
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import anthropic
import fitz  # PyMuPDF - for PDF support
import requests as http_requests  # renamed to avoid conflict with flask.request

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max upload

EXTRACTION_PROMPT = """You are an expert at reading electrical supply house tickets and invoices.

Look at this supply house ticket image and extract ALL line items and header information.

CRITICAL RULES FOR LINE ITEM SEPARATION:
- Each row that has its OWN AMOUNT (dollar value) in the rightmost column is a SEPARATE line item
- NEVER combine two rows that each have their own AMOUNT into one line item
- Items with different U/M (unit of measure) like EA vs M are ALWAYS separate items
- Multi-line descriptions (where a second line has NO amount) belong to the item above them
- The ORDERED column (leftmost) shows the quantity — use this for qty
- If there is a SHIPPED column with a value, use SHIPPED instead of ORDERED
- Read the UNIT PRICE and AMOUNT columns carefully — they are separate columns

HOW TO IDENTIFY SEPARATE LINE ITEMS:
1. Look at the AMOUNT column (rightmost dollar column)
2. Every row that has a number in the AMOUNT column = one separate line item
3. Rows between amount rows that have NO amount = continuation of the description above
4. Example: "BLI B2211-1-1/4PAZN" with amount 209.64 is ONE item, then "WIR 14 THHN STR BLK @ 500" with amount 106.40 is a DIFFERENT item

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{
  "header": {
    "supplier": "supplier name if visible",
    "shipping_number": "shipping/invoice/quote number",
    "customer": "customer name",
    "order_date": "order date",
    "date": "ticket date",
    "page": "page X of Y if visible"
  },
  "line_items": [
    {
      "qty": 100,
      "description": "BLI B2211-1-1/4PAZN 1-1/4 EMT/RIG STRUT STRAP",
      "unit_of_measure": "EA",
      "unit_price": 2.0964,
      "amount": 209.64,
      "package_size": 1,
      "package_unit": "EA",
      "num_packages": 100,
      "cost_per_package": 2.0964
    },
    {
      "qty": 1000,
      "description": "WIR 14 THHN STR BLK @ 500 14GA STR-BLACK @ 500 FT SPOOL",
      "unit_of_measure": "M",
      "unit_price": 106.4000,
      "amount": 106.40,
      "package_size": 500,
      "package_unit": "500 FT SPOOL",
      "num_packages": 2,
      "cost_per_package": 53.20
    }
  ],
  "totals": {
    "subtotal": 0,
    "tax": 0,
    "total": 0
  }
}

PACKAGING AND UNIT CONVERSION RULES:
The supply house uses 3 units of measure:
- EA = price per each (1 unit)
- C = price per 100
- M = price per 1000

You MUST figure out how the item is actually sold/packaged and calculate:
- "package_size": how many units per package (look for "@ 500", "@ 1000", "@ 2500" in description, or if EA then 1)
- "package_unit": what one package is called (e.g., "500 FT SPOOL", "1000 FT REEL", "EA", "BOX OF 100")
- "num_packages": total qty divided by package_size = how many packages were bought
- "cost_per_package": amount divided by num_packages = cost of one package

Examples:
- "WIR 14 THHN STR BLK @ 500" with qty=1000, amount=106.40
  → package_size=500, num_packages=2, cost_per_package=53.20 (each 500ft spool costs $53.20)
- "WIR 1 THHN STR @ 1000" with qty=3000, amount=6776.41
  → package_size=1000, num_packages=3, cost_per_package=2258.80 (each 1000ft reel costs $2258.80)
- "CON ALU 3/4" (conduit) with qty=1050, amount=1465.65, U/M=C
  → package_size=1 (sold per stick/each), num_packages=1050, cost_per_package=1.3958
- "BLI B2211" with qty=100, U/M=EA, amount=209.64
  → package_size=1, num_packages=100, cost_per_package=2.0964
- If no "@ XXX" in description and U/M is M: this means wire sold by the foot, priced per 1000ft.
  → package_size=1, package_unit="FT", num_packages=qty (the actual footage), cost_per_package=amount/qty (price per foot)
  Example: 674 ft of wire, U/M=M, unit_price=928.3078, amount=625.48
  → package_size=1, package_unit="FT", num_packages=674, cost_per_package=0.9283 (per foot)
- If no "@ XXX" in description and U/M is C: sold per piece, priced per 100.
  → package_size=1, package_unit="EA", num_packages=qty, cost_per_package=amount/qty (price per piece)
- If U/M is EA: package_size=1, package_unit="EA", num_packages=qty, cost_per_package=amount/qty

Additional rules:
- Include ALL line items — do not skip any
- Prices and amounts should be numbers, not strings
- Read part numbers carefully: B2211 not R2711, B2408 not R2408, etc.
- If a description spans multiple lines but has only ONE amount, combine into one description
- If quantity is unclear, check if ORDERED and SHIPPED columns differ
- Return ONLY the JSON, nothing else"""


@app.route('/')
def index():
    has_key = bool(os.environ.get('ANTHROPIC_API_KEY'))
    return render_template('index.html', has_api_key=has_key)


@app.route('/extract', methods=['POST'])
def extract():
    api_key = request.form.get('api_key') or os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'No API key provided. Enter your Anthropic API key in the settings.'}), 400

    files = request.files.getlist('images')
    if not files:
        return jsonify({'error': 'No files uploaded'}), 400

    client = anthropic.Anthropic(api_key=api_key)

    all_items = []
    header_info = {}
    totals_info = {}
    errors = []

    # Build a list of (image_bytes, media_type, label) for all pages
    pages_to_process = []

    for file in files:
        filename = file.filename.lower()
        file_bytes = file.read()

        if filename.endswith('.pdf'):
            # Convert each PDF page to a PNG image
            try:
                pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
                for page_num in range(len(pdf_doc)):
                    page = pdf_doc[page_num]
                    # Render at 2x resolution for better OCR accuracy
                    mat = fitz.Matrix(2, 2)
                    pix = page.get_pixmap(matrix=mat)
                    img_bytes = pix.tobytes("png")
                    img_b64 = base64.standard_b64encode(img_bytes).decode('utf-8')
                    pages_to_process.append((img_b64, 'image/png', f"{file.filename} page {page_num + 1}"))
                pdf_doc.close()
            except Exception as e:
                errors.append(f"PDF error ({file.filename}): {str(e)}")
        else:
            # Regular image file
            img_b64 = base64.standard_b64encode(file_bytes).decode('utf-8')
            if filename.endswith('.png'):
                media_type = 'image/png'
            elif filename.endswith('.gif'):
                media_type = 'image/gif'
            elif filename.endswith('.webp'):
                media_type = 'image/webp'
            else:
                media_type = 'image/jpeg'
            pages_to_process.append((img_b64, media_type, f"{file.filename}"))

    for i, (image_data, media_type, label) in enumerate(pages_to_process):
        try:
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": image_data,
                                },
                            },
                            {
                                "type": "text",
                                "text": EXTRACTION_PROMPT
                            }
                        ],
                    }
                ],
            )

            response_text = message.content[0].text.strip()

            # Clean up response - remove markdown code fences if present
            if response_text.startswith('```'):
                response_text = response_text.split('\n', 1)[1]
                if response_text.endswith('```'):
                    response_text = response_text[:-3].strip()

            data = json.loads(response_text)

            # Merge header from first page
            if not header_info and 'header' in data:
                header_info = data['header']

            # Accumulate line items
            if 'line_items' in data:
                all_items.extend(data['line_items'])

            # Keep latest totals
            if 'totals' in data:
                totals_info = data['totals']

        except json.JSONDecodeError as e:
            errors.append(f"{label}: Could not parse response as JSON - {str(e)}")
        except anthropic.APIError as e:
            errors.append(f"{label}: API error - {str(e)}")
        except Exception as e:
            errors.append(f"{label}: {str(e)}")

    result = {
        'header': header_info,
        'line_items': all_items,
        'totals': totals_info,
        'pages_processed': len(pages_to_process),
        'errors': errors
    }

    return jsonify(result)


@app.route('/save-key', methods=['POST'])
def save_key():
    """Save API key to .env file for persistence."""
    data = request.get_json()
    api_key = data.get('api_key', '').strip()
    if not api_key:
        return jsonify({'error': 'No key provided'}), 400

    env_path = os.path.join(os.path.dirname(__file__), '.env')

    # Read existing env to preserve other keys
    existing = {}
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    existing[k] = v

    existing['ANTHROPIC_API_KEY'] = api_key

    with open(env_path, 'w') as f:
        for k, v in existing.items():
            f.write(f'{k}={v}\n')

    os.environ['ANTHROPIC_API_KEY'] = api_key
    return jsonify({'success': True})


@app.route('/save-ninja-settings', methods=['POST'])
def save_ninja_settings():
    """Save Invoice Ninja settings."""
    data = request.get_json()
    ninja_url = data.get('ninja_url', '').strip()
    ninja_token = data.get('ninja_token', '').strip()

    env_path = os.path.join(os.path.dirname(__file__), '.env')
    existing = {}
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    existing[k] = v

    if ninja_url:
        existing['NINJA_URL'] = ninja_url
    if ninja_token:
        existing['NINJA_TOKEN'] = ninja_token

    with open(env_path, 'w') as f:
        for k, v in existing.items():
            f.write(f'{k}={v}\n')

    return jsonify({'success': True})


@app.route('/get-clients', methods=['GET'])
def get_clients():
    """Fetch client list from Invoice Ninja for the dropdown."""
    ninja_url = request.args.get('ninja_url') or os.environ.get('NINJA_URL', 'http://localhost:8080')
    ninja_token = request.args.get('ninja_token') or os.environ.get('NINJA_TOKEN', '')

    if not ninja_token:
        return jsonify({'error': 'No Invoice Ninja API token configured.'}), 400

    headers = {
        'X-Api-Token': ninja_token,
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
    }

    try:
        all_clients = []
        page = 1
        while True:
            resp = http_requests.get(
                f'{ninja_url}/api/v1/clients?per_page=100&page={page}',
                headers=headers, timeout=10
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            clients = data.get('data', [])
            if not clients:
                break
            for c in clients:
                all_clients.append({
                    'id': c['id'],
                    'name': c.get('name', ''),
                    'city': c.get('city', ''),
                })
            # Check if there are more pages
            meta = data.get('meta', {}).get('pagination', {})
            if page >= meta.get('total_pages', 1):
                break
            page += 1

        all_clients.sort(key=lambda x: x['name'].lower())
        return jsonify({'clients': all_clients})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/create-client', methods=['POST'])
def create_client():
    """Create a new client in Invoice Ninja."""
    data = request.get_json()
    ninja_url = data.get('ninja_url') or os.environ.get('NINJA_URL', 'http://localhost:8080')
    ninja_token = data.get('ninja_token') or os.environ.get('NINJA_TOKEN', '')
    name = data.get('name', '').strip()

    if not ninja_token:
        return jsonify({'error': 'No Invoice Ninja API token configured.'}), 400
    if not name:
        return jsonify({'error': 'Client name is required.'}), 400

    headers = {
        'X-Api-Token': ninja_token,
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
    }

    try:
        resp = http_requests.post(
            f'{ninja_url}/api/v1/clients',
            json={'name': name},
            headers=headers,
            timeout=10
        )
        if resp.status_code in [200, 201]:
            client_id = resp.json().get('data', {}).get('id')
            return jsonify({'success': True, 'client_id': client_id})
        else:
            return jsonify({'error': f'Failed: {resp.text[:200]}'}), resp.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/create-invoice', methods=['POST'])
def create_invoice():
    """Create a draft invoice in Invoice Ninja with extracted line items."""
    data = request.get_json()
    ninja_url = data.get('ninja_url') or os.environ.get('NINJA_URL', 'http://localhost:8080')
    ninja_token = data.get('ninja_token') or os.environ.get('NINJA_TOKEN', '')

    if not ninja_token:
        return jsonify({'error': 'No Invoice Ninja API token configured. Add it in Settings.'}), 400

    line_items = data.get('line_items', [])
    job_name = data.get('job_name', 'Untitled Job')
    client_id = data.get('client_id', '')
    apply_tax = data.get('apply_tax', True)

    if not client_id:
        return jsonify({'error': 'Please select a client from the dropdown before sending.'}), 400

    # Build Invoice Ninja line items
    ninja_items = []
    for item in line_items:
        qty = float(item.get('qty', 1)) or 1
        line_cost = float(item.get('line_cost', 0))
        markup_pct = float(item.get('markup', 0))
        marked_up_total = line_cost * (1 + markup_pct / 100)
        desc = item.get('description', '')
        item_name = desc.split('@')[0].split('14GA')[0].split('12GA')[0].strip()
        if len(item_name) > 40:
            item_name = item_name[:40].strip()
        item_data = {
            'product_key': item_name,
            'notes': f"Qty: {int(qty)} - {desc}",
            'quantity': 1,
            'cost': round(marked_up_total, 2),
        }
        if apply_tax:
            item_data['tax_name1'] = 'Arkansas State Tax'
            item_data['tax_rate1'] = 9
        ninja_items.append(item_data)

    # API headers
    headers = {
        'X-Api-Token': ninja_token,
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
    }

    # Create the invoice
    invoice_data = {
        'client_id': client_id,
        'line_items': ninja_items,
        'public_notes': f'Job: {job_name}',
        'private_notes': f'Source: Supply ticket scanner\nCustomer: {customer_name}',
        'status_id': '1',  # Draft
    }

    try:
        resp = http_requests.post(
            f'{ninja_url}/api/v1/invoices',
            json=invoice_data,
            headers=headers,
            timeout=15
        )

        if resp.status_code in [200, 201]:
            result = resp.json()
            invoice = result.get('data', {})
            return jsonify({
                'success': True,
                'invoice_id': invoice.get('id'),
                'invoice_number': invoice.get('number'),
                'total': invoice.get('amount'),
                'url': f'{ninja_url}/invoices/{invoice.get("id")}/edit'
            })
        else:
            return jsonify({'error': f'Invoice Ninja error ({resp.status_code}): {resp.text[:300]}'}), resp.status_code

    except Exception as e:
        return jsonify({'error': f'Could not connect to Invoice Ninja: {str(e)}'}), 500


if __name__ == '__main__':
    print("\n" + "="*50)
    print("  SUPPLY TICKET SCANNER")
    print("="*50)
    print("\n  Open your browser to: http://localhost:5000")
    print("  Press Ctrl+C to stop\n")
    app.run(debug=True, port=5000)
