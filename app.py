import os
import json
import base64
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import anthropic

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
      "amount": 209.64
    },
    {
      "qty": 1000,
      "description": "WIR 14 THHN STR BLK @ 500 14GA STR-BLACK @ 500 FT SPOOL",
      "unit_of_measure": "M",
      "unit_price": 106.4000,
      "amount": 106.40
    }
  ],
  "totals": {
    "subtotal": 0,
    "tax": 0,
    "total": 0
  }
}

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
        return jsonify({'error': 'No images uploaded'}), 400

    client = anthropic.Anthropic(api_key=api_key)

    all_items = []
    header_info = {}
    totals_info = {}
    errors = []

    for i, file in enumerate(files):
        try:
            image_data = base64.standard_b64encode(file.read()).decode('utf-8')

            # Detect media type
            filename = file.filename.lower()
            if filename.endswith('.png'):
                media_type = 'image/png'
            elif filename.endswith('.gif'):
                media_type = 'image/gif'
            elif filename.endswith('.webp'):
                media_type = 'image/webp'
            else:
                media_type = 'image/jpeg'

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
            errors.append(f"Page {i+1}: Could not parse response as JSON - {str(e)}")
        except anthropic.APIError as e:
            errors.append(f"Page {i+1}: API error - {str(e)}")
        except Exception as e:
            errors.append(f"Page {i+1}: {str(e)}")

    result = {
        'header': header_info,
        'line_items': all_items,
        'totals': totals_info,
        'pages_processed': len(files),
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
    with open(env_path, 'w') as f:
        f.write(f'ANTHROPIC_API_KEY={api_key}\n')

    os.environ['ANTHROPIC_API_KEY'] = api_key
    return jsonify({'success': True})


if __name__ == '__main__':
    print("\n" + "="*50)
    print("  SUPPLY TICKET SCANNER")
    print("="*50)
    print("\n  Open your browser to: http://localhost:5000")
    print("  Press Ctrl+C to stop\n")
    app.run(debug=True, port=5000)
