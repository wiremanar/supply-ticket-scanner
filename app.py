import os
import json
import base64
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import anthropic

load_dotenv()

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max upload

EXTRACTION_PROMPT = """You are an expert at reading electrical supply house tickets/invoices.

Look at this supply house ticket image and extract ALL line items and header information.

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{
  "header": {
    "supplier": "supplier name if visible",
    "shipping_number": "shipping/invoice number",
    "customer": "customer name",
    "order_date": "order date",
    "date": "ticket date",
    "page": "page X of Y if visible"
  },
  "line_items": [
    {
      "qty": 500,
      "description": "WIR 6 THHN STR GRN @ 500",
      "unit_of_measure": "M",
      "unit_price": 928.3078,
      "amount": 454.15
    }
  ],
  "totals": {
    "subtotal": 13445.10,
    "tax": 1142.83,
    "total": 14587.93
  }
}

Rules:
- Use the SHIPPED quantity (not ordered quantity) for qty
- Include ALL line items, even partial ones
- Use the exact description text from the ticket
- Prices should be numbers, not strings
- If a value is unclear, use your best reading and flag with a note
- For multi-line descriptions, combine into one description string
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
                model="claude-sonnet-4-6-20250514",
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
