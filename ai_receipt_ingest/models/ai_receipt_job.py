import base64
import json
import logging
from mimetypes import guess_type
from urllib import error, request

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


EXTRACTION_SCHEMA = {
    "name": "receipt_extraction",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "document_type": {
                "type": "string",
                "enum": ["vendor_bill", "receipt", "other"]
            },
            "vendor_name": {"type": ["string", "null"]},
            "invoice_number": {"type": ["string", "null"]},
            "po_number": {"type": ["string", "null"]},
            "invoice_date": {"type": ["string", "null"]},
            "due_date": {"type": ["string", "null"]},
            "currency": {"type": ["string", "null"]},
            "subtotal": {"type": ["number", "null"]},
            "tax_amount": {"type": ["number", "null"]},
            "total_amount": {"type": ["number", "null"]},
            "payment_reference": {"type": ["string", "null"]},
            "summary": {"type": ["string", "null"]},
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": {"type": ["number", "null"]},
                        "unit_price": {"type": ["number", "null"]},
                        "amount": {"type": ["number", "null"]}
                    },
                    "required": ["description", "quantity", "unit_price", "amount"],
                    "additionalProperties": False
                }
            },
            "confidence_notes": {"type": ["string", "null"]}
        },
        "required": [
            "document_type", "vendor_name", "invoice_number", "po_number",
            "invoice_date", "due_date", "currency", "subtotal", "tax_amount",
            "total_amount", "payment_reference", "summary", "line_items", "confidence_notes"
        ],
        "additionalProperties": False
    }
}

CLASSIFICATION_SCHEMA = {
    "name": "line_account_classification",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "account_code": {"type": "string"},
                        "reason": {"type": ["string", "null"]}
                    },
                    "required": ["index", "account_code", "reason"],
                    "additionalProperties": False
                }
            }
        },
        "required": ["mappings"],
        "additionalProperties": False
    }
}


class AiReceiptJob(models.Model):
    _name = 'ai.receipt.job'
    _description = 'AI Receipt Extraction Job'
    _order = 'id desc'

    name = fields.Char(required=True, default='New Receipt Job')
    state = fields.Selection([
        ('draft', 'Draft'),
        ('analyzed', 'Analyzed'),
        ('done', 'Vendor Bill Created'),
        ('error', 'Error'),
    ], default='draft')

    file_name = fields.Char(required=True)
    file_data = fields.Binary(required=True, attachment=True)
    mime_type = fields.Char(compute='_compute_mime_type', store=True)

    extracted_json = fields.Text(readonly=True)
    extraction_summary = fields.Text(readonly=True)
    error_message = fields.Text(readonly=True)

    partner_id = fields.Many2one('res.partner', string='Vendor', readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    invoice_date = fields.Date(readonly=True)
    due_date = fields.Date(readonly=True)
    invoice_number = fields.Char(readonly=True)
    payment_reference = fields.Char(readonly=True)
    total_amount = fields.Monetary(currency_field='currency_id', readonly=True)
    subtotal_amount = fields.Monetary(currency_field='currency_id', readonly=True)
    tax_amount = fields.Monetary(currency_field='currency_id', readonly=True)
    bill_id = fields.Many2one('account.move', string='Created Vendor Bill', readonly=True)

    @api.depends('file_name')
    def _compute_mime_type(self):
        for rec in self:
            rec.mime_type = guess_type(rec.file_name or '')[0] or 'application/octet-stream'

    def _get_config(self):
        icp = self.env['ir.config_parameter'].sudo()
        api_key = icp.get_param('ai_receipt_ingest.openai_api_key')
        model = icp.get_param('ai_receipt_ingest.openai_model') or 'gpt-4.1-mini'
        default_account_id = icp.get_param('ai_receipt_ingest.default_expense_account_id')
        default_journal_id = icp.get_param('ai_receipt_ingest.default_purchase_journal_id')
        auto_create_partner = icp.get_param('ai_receipt_ingest.auto_create_partner', 'True') == 'True'
        return {
            'api_key': api_key,
            'model': model,
            'default_account_id': int(default_account_id) if default_account_id else False,
            'default_journal_id': int(default_journal_id) if default_journal_id else False,
            'auto_create_partner': auto_create_partner,
            'asset_threshold': 500.0,
        }

    def _get_raw_file_bytes(self):
        self.ensure_one()
        if not self.file_data:
            raise UserError(_('Upload a file first.'))
        raw = self.file_data
        if isinstance(raw, str):
            raw = raw.encode('utf-8')
        try:
            return base64.b64decode(raw)
        except Exception as exc:
            raise UserError(_('Could not decode the uploaded file: %s') % exc)

    def _upload_openai_file(self, api_key):
        self.ensure_one()
        file_bytes = self._get_raw_file_bytes()
        boundary = '----OdooOpenAIBoundary7MA4YWxkTrZu0gW'
        lines = []
        lines.append(f'--{boundary}'.encode('utf-8'))
        lines.append(b'Content-Disposition: form-data; name="purpose"')
        lines.append(b'')
        lines.append(b'user_data')
        lines.append(f'--{boundary}'.encode('utf-8'))
        disposition = (
            f'Content-Disposition: form-data; name="file"; filename="{self.file_name or "receipt"}"'
        )
        lines.append(disposition.encode('utf-8'))
        lines.append(f'Content-Type: {self.mime_type or "application/octet-stream"}'.encode('utf-8'))
        lines.append(b'')
        lines.append(file_bytes)
        lines.append(f'--{boundary}--'.encode('utf-8'))
        lines.append(b'')

        body = b'\r\n'.join(lines)
        req = request.Request(
            'https://api.openai.com/v1/files',
            data=body,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': f'multipart/form-data; boundary={boundary}',
            },
            method='POST',
        )
        try:
            with request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode('utf-8'))
        except error.HTTPError as exc:
            raw = exc.read().decode('utf-8', errors='ignore')
            _logger.exception('OpenAI file upload HTTP error: %s', raw)
            raise UserError(_('OpenAI file upload failed: %s') % raw)
        except Exception as exc:
            _logger.exception('OpenAI file upload failed')
            raise UserError(_('OpenAI file upload failed: %s') % exc)

        file_id = result.get('id')
        if not file_id:
            raise UserError(_('OpenAI file upload did not return a file id.'))
        return file_id

    def _openai_responses_request(self, payload, api_key, timeout=120):
        req = request.Request(
            'https://api.openai.com/v1/responses',
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}',
            },
            method='POST',
        )
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode('utf-8'))
        except error.HTTPError as exc:
            raw = exc.read().decode('utf-8', errors='ignore')
            _logger.exception('OpenAI Responses HTTP error: %s', raw)
            raise UserError(_('OpenAI request failed: %s') % raw)
        except Exception as exc:
            _logger.exception('OpenAI Responses request failed')
            raise UserError(_('OpenAI request failed: %s') % exc)

        output_text = None
        for item in body.get('output', []):
            if item.get('type') == 'message':
                for content in item.get('content', []):
                    if content.get('type') == 'output_text':
                        output_text = content.get('text')
                        break
                if output_text:
                    break

        if not output_text:
            raise UserError(_('No structured JSON was returned by OpenAI.'))
        return json.loads(output_text)

    def _call_openai_extract(self):
        self.ensure_one()
        config = self._get_config()
        if not config['api_key']:
            raise UserError(_('Set your OpenAI API key in Settings first.'))

        prompt = (
            'Extract structured bookkeeping data from this uploaded bill, invoice, or receipt. '
            'Return only the requested JSON schema. '
            'Dates must be YYYY-MM-DD when visible; otherwise null. '
            'Currency should be a 3-letter ISO code when clear; otherwise null. '
            'Do not invent missing values. '
            'For line_items, include best-effort descriptions and amounts. '
            'Document type should be vendor_bill for supplier invoices, receipt for store receipts, or other if unclear.'
        )

        file_id = self._upload_openai_file(config['api_key'])
        payload = {
            'model': config['model'],
            'input': [{
                'role': 'user',
                'content': [
                    {'type': 'input_text', 'text': prompt},
                    {'type': 'input_file', 'file_id': file_id},
                ],
            }],
            'text': {
                'format': {
                    'type': 'json_schema',
                    'name': EXTRACTION_SCHEMA['name'],
                    'strict': EXTRACTION_SCHEMA['strict'],
                    'schema': EXTRACTION_SCHEMA['schema'],
                }
            }
        }
        return self._openai_responses_request(payload, config['api_key'], timeout=120)

    def action_analyze(self):
        for rec in self:
            try:
                data = rec._call_openai_extract()
                rec._apply_extraction(data)
            except Exception as exc:
                rec.write({
                    'state': 'error',
                    'error_message': str(exc),
                })
                raise
        return True

    def _find_or_create_partner(self, vendor_name, auto_create_partner):
        self.ensure_one()
        if not vendor_name:
            return False
        partner = self.env['res.partner'].search([('name', '=ilike', vendor_name)], limit=1)
        if partner or not auto_create_partner:
            return partner
        return self.env['res.partner'].create({'name': vendor_name, 'supplier_rank': 1})

    def _apply_extraction(self, data):
        self.ensure_one()
        config = self._get_config()
        currency = False
        if data.get('currency'):
            currency = self.env['res.currency'].search([('name', '=', data['currency'])], limit=1)

        partner = self._find_or_create_partner(data.get('vendor_name'), config['auto_create_partner'])
        self.write({
            'name': data.get('vendor_name') or self.file_name,
            'state': 'analyzed',
            'extracted_json': json.dumps(data, indent=2),
            'extraction_summary': data.get('summary') or data.get('confidence_notes'),
            'partner_id': partner.id if partner else False,
            'currency_id': currency.id if currency else self.env.company.currency_id.id,
            'invoice_date': data.get('invoice_date') or False,
            'due_date': data.get('due_date') or False,
            'invoice_number': data.get('invoice_number') or False,
            'payment_reference': data.get('payment_reference') or False,
            'total_amount': data.get('total_amount') or 0.0,
            'subtotal_amount': data.get('subtotal') or 0.0,
            'tax_amount': data.get('tax_amount') or 0.0,
            'error_message': False,
        })

    def _all_non_deprecated_accounts(self):
        # Odoo 19 on this install does not expose a 'deprecated' field on account.account.
        # Keep it broad and company-safe rather than filtering on a field that may not exist.
        domain = []
        if 'company_ids' in self.env['account.account']._fields:
            domain = [('company_ids', 'in', [self.env.company.id])]
        elif 'company_id' in self.env['account.account']._fields:
            domain = ['|', ('company_id', '=', False), ('company_id', '=', self.env.company.id)]
        return self.env['account.account'].search(domain)

    def _find_account_by_keywords(self, keywords, accounts=None):
        accounts = accounts or self._all_non_deprecated_accounts()
        keyword_l = [k.lower() for k in keywords]
        for account in accounts:
            hay = ' '.join(filter(None, [account.code or '', account.name or ''])).lower()
            if all(word in hay for word in keyword_l):
                return account
        return False

    def _find_account_by_exact_names(self, names, accounts=None):
        accounts = accounts or self._all_non_deprecated_accounts()
        wanted = {name.strip().lower() for name in names if name}
        for account in accounts:
            if (account.name or '').strip().lower() in wanted:
                return account
        return False

    def _build_policy_accounts(self, fallback_account):
        accounts = self._all_non_deprecated_accounts()

        exact_name_map = {
            'raw_materials': ['Raw Materials'],
            'misc_expenses': ['Miscellaneous Expenses'],
            'office_supplies': ['Office Supplies'],
            'machines_tools': ['Machines & Tools'],
            'technology': ['Technology', 'Computers'],
            'software': ['Software'],
            'advertising_marketing': ['Advertising & Marketing'],
            'fuel': ['Fuel'],
            'meals_entertainment': ['Meals & Entertainment'],
            'food_catering_delivery': ['Food: Catering & Delivery'],
            'ground_transportation': ['Ground Transportation'],
            'public_transportation': ['Public Transportation'],
            'airfare': ['Airfare'],
            'hotels': ['Hotels'],
            'vehicles_expense': ['Vehicles', 'Vehicle Rent', 'Auto Insurance'],
            'licenses_permits': ['Licenses and Permits'],
            'fixed_assets_technology': ['Technology'],
            'fixed_assets_machines_tools': ['Machines & Tools'],
            'fixed_assets_vehicles': ['Vehicles'],
            'fixed_assets_misc': ['Miscellaneous Fixed Assets'],
        }

        preferred = {
            'raw_materials': [
                ['raw', 'materials'],
            ],
            'misc_expenses': [
                ['miscellaneous', 'expenses'],
                ['misc', 'expenses'],
            ],
            'office_supplies': [
                ['office', 'supplies'],
            ],
            'machines_tools': [
                ['machines', 'tools'],
                ['machine', 'tools'],
            ],
            'technology': [
                ['technology'],
                ['computers'],
            ],
            'software': [
                ['software'],
            ],
            'advertising_marketing': [
                ['advertising', 'marketing'],
                ['marketing'],
            ],
            'fuel': [
                ['fuel'],
            ],
            'meals_entertainment': [
                ['meals', 'entertainment'],
            ],
            'food_catering_delivery': [
                ['food', 'catering'],
                ['catering', 'delivery'],
            ],
            'ground_transportation': [
                ['ground', 'transportation'],
            ],
            'public_transportation': [
                ['public', 'transportation'],
            ],
            'airfare': [
                ['airfare'],
            ],
            'hotels': [
                ['hotels'],
                ['hotel'],
            ],
            'vehicles_expense': [
                ['vehicles'],
                ['vehicle', 'rent'],
                ['auto', 'insurance'],
            ],
            'licenses_permits': [
                ['licenses', 'permits'],
                ['license'],
            ],
            'fixed_assets_technology': [
                ['technology'],
            ],
            'fixed_assets_machines_tools': [
                ['machines', 'tools'],
                ['machine', 'tools'],
            ],
            'fixed_assets_vehicles': [
                ['vehicles'],
            ],
            'fixed_assets_misc': [
                ['miscellaneous', 'fixed', 'assets'],
                ['misc', 'fixed', 'assets'],
            ],
        }

        bucket_accounts = {}
        for bucket, keyword_sets in preferred.items():
            found = self._find_account_by_exact_names(exact_name_map.get(bucket, []), accounts=accounts)
            if not found:
                for keyword_set in keyword_sets:
                    found = self._find_account_by_keywords(keyword_set, accounts=accounts)
                    if found:
                        break
            if found:
                bucket_accounts[bucket] = found

        fallback = fallback_account or bucket_accounts.get('misc_expenses')
        if not fallback:
            fallback = accounts[:1]

        candidates = []
        seen = set()
        ordered_buckets = [
            ('raw_materials', 'Direct materials or production consumables'),
            ('misc_expenses', 'General shop consumables or uncertain fallback'),
            ('office_supplies', 'Office and admin supplies'),
            ('machines_tools', 'Small durable shop tools and equipment under the capitalization threshold'),
            ('technology', 'Technology and electronics under the capitalization threshold'),
            ('software', 'Software and digital tools'),
            ('advertising_marketing', 'Advertising, branding, and promotional costs'),
            ('fuel', 'Fuel and fuel-like transportation spending'),
            ('meals_entertainment', 'Meals and entertainment'),
            ('food_catering_delivery', 'Food, catering, and delivery'),
            ('ground_transportation', 'Ground transportation and rides'),
            ('public_transportation', 'Public transportation'),
            ('airfare', 'Airfare'),
            ('hotels', 'Hotels and lodging'),
            ('vehicles_expense', 'Vehicle-related operating expenses'),
            ('licenses_permits', 'Licenses and permits'),
            ('fixed_assets_machines_tools', 'Capitalized tools or equipment at or above the threshold'),
            ('fixed_assets_technology', 'Capitalized technology/electronics at or above the threshold'),
            ('fixed_assets_vehicles', 'Capitalized vehicle purchases'),
            ('fixed_assets_misc', 'Other fixed assets at or above the threshold'),
        ]
        for bucket, purpose in ordered_buckets:
            account = bucket_accounts.get(bucket)
            if account and account.id not in seen:
                candidates.append({
                    'bucket': bucket,
                    'code': account.code,
                    'name': account.name,
                    'purpose': purpose,
                    'id': account.id,
                })
                seen.add(account.id)

        if fallback and fallback.id not in seen:
            candidates.append({
                'bucket': 'fallback',
                'code': fallback.code,
                'name': fallback.name,
                'purpose': 'Fallback account when classification is uncertain',
                'id': fallback.id,
            })
        return {
            'fallback': fallback,
            'candidates': candidates,
        }

    def _heuristic_bucket_for_line(self, description, amount, threshold):
        text = (description or '').lower()
        amount = amount or 0.0

        raw_material_keywords = [
            'copper', 'silver', 'brass', 'gold', 'bronze', 'shibuichi', 'shakudo', 'mesh',
            'wire', 'sheet', 'leather', 'hide', 'resin', 'wax', 'pine resin', 'beeswax',
            'patina', 'rokusho', 'sulfur', 'glue', 'epoxy', 'pigment', 'dye', 'stone',
            'gem', 'gemstone', 'chain', 'findings', 'bead', 'fossil', 'bone', 'wood blank',
            'metal powder', 'powder', 'casting grain', 'liver of sulfur', 'patina', 'oxidized', 'oxidizer',
          'blackener', 'antique finish', 'metal finish', 'surface treatment','etch', 'etching', 'pickle',
          'flux', 'soldering flux', 'jewelry making', 'jewelry projects', 'metal working', 'apply a patina',
        ]
        misc_keywords = [
            'brush', 'brushes', 'disposable', 'applicator', 'glue brush', 'flux brush',
        ]
        office_keywords = [
            'paper', 'notebook', 'pen', 'pencil', 'staple', 'label', 'printer paper', 'envelope',
        ]
        software_keywords = [
            'software', 'subscription', 'saas', 'license', 'cloud', 'adobe', 'fusion 360', 'slicer',
        ]
        marketing_keywords = [
            'advertising', 'promotion', 'promotional', 'marketing', 'business card',
            'flyer', 'banner', 'booth',
        ]
        travel_ground_keywords = ['uber', 'lyft', 'taxi', 'parking', 'toll', 'ground transportation']
        public_transport_keywords = ['bus', 'train', 'subway', 'metro', 'public transportation']
        airfare_keywords = ['airfare', 'flight', 'airline']
        hotel_keywords = ['hotel', 'lodging', 'inn', 'motel']
        fuel_keywords = ['fuel', 'gas', 'gasoline', 'diesel']
        meals_keywords = ['restaurant', 'meal', 'meals', 'entertainment']
        food_keywords = ['catering', 'delivery']
        vehicle_keywords = ['vehicle', 'truck', 'trailer', 'auto insurance', 'car']
        machines_tools_keywords = [
            'tool', 'pliers', 'drill', 'saw', 'hammer', 'wrench', 'anvil', 'torch', 'laser',
            'printer', 'spoon set', 'brush set', 'measuring spoon', 'caliper', 'vise',
        ]
        tech_keywords = ['computer', 'laptop', 'tablet', 'monitor', 'camera', 'phone', 'technology']

        if any(word in text for word in raw_material_keywords):
            return 'raw_materials'

        if any(word in text for word in machines_tools_keywords):
            return 'fixed_assets_machines_tools' if amount >= threshold else 'machines_tools'

        if any(word in text for word in software_keywords):
            return 'software'

        if any(word in text for word in office_keywords):
            return 'office_supplies'

        if any(word in text for word in misc_keywords):
            return 'misc_expenses'

       
        if any(word in text for word in vehicle_keywords):
            return 'fixed_assets_vehicles' if amount >= threshold else 'vehicles_expense'

        if any(word in text for word in tech_keywords):
            return 'fixed_assets_technology' if amount >= threshold else 'technology'

        
        if any(word in text for word in marketing_keywords):
            return 'advertising_marketing'
        if any(word in text for word in airfare_keywords):
            return 'airfare'
        if any(word in text for word in hotel_keywords):
            return 'hotels'
        if any(word in text for word in public_transport_keywords):
            return 'public_transportation'
        if any(word in text for word in travel_ground_keywords):
            return 'ground_transportation'
        if any(word in text for word in fuel_keywords):
            return 'fuel'
        if any(word in text for word in meals_keywords):
            return 'meals_entertainment'
        if any(word in text for word in food_keywords):
            return 'food_catering_delivery'

        return 'misc_expenses'

    def _heuristic_account_mapping(self, line_items, policy_accounts, threshold):
        candidates_by_bucket = {item['bucket']: item for item in policy_accounts['candidates']}
        fallback = policy_accounts['fallback']
        mappings = []
        for idx, item in enumerate(line_items):
            amount = item.get('amount')
            if amount is None:
                qty = item.get('quantity') or 1.0
                price_unit = item.get('unit_price') or 0.0
                amount = qty * price_unit

            bucket = self._heuristic_bucket_for_line(item.get('description'), amount, threshold)
            selected = candidates_by_bucket.get(bucket)
            if not selected:
                selected = candidates_by_bucket.get('misc_expenses')
            if not selected:
                selected = {
                    'code': fallback.code if fallback else '',
                    'name': fallback.name if fallback else '',
                    'id': fallback.id if fallback else False,
                    'bucket': 'fallback',
                }
            mappings.append({
                'index': idx,
                'account_code': selected['code'],
                'reason': 'Local policy fallback via keyword rules.',
            })
        return mappings

    def _call_openai_account_classification(self, line_items, policy_accounts, config):
        if not config['api_key']:
            return []

        prompt_lines = [
            'Classify each extracted vendor bill line into one of the provided Odoo account codes.',
            'Use only the provided account codes. Never invent a code.',
            'Apply this business policy strictly:',
            f'- Durable tools, machines, equipment, technology, or vehicles with a per-line amount >= ${config["asset_threshold"]:.2f} should be capitalized using the most appropriate fixed asset account.',
            '- Materials that become part of the product or are directly consumed in production belong in Raw Materials.',
            '- Small shop support items and unclear shop consumables belong in Miscellaneous Expenses.',
            '- Office/admin-only supplies belong in Office Supplies.',
            '- Software and digital tools belong in Software.',
            '- Advertising/promotional purchases belong in Advertising & Marketing.',
            '- Travel/fuel/meals categories should only be used when the line clearly indicates them.',
            f'- If uncertain, choose fallback code {policy_accounts["fallback"].code if policy_accounts["fallback"] else ""}.',
        ]

        candidate_lines = [
            f'{item["code"]} | {item["name"]} | {item["purpose"]}'
            for item in policy_accounts['candidates']
        ]
        item_lines = []
        for idx, item in enumerate(line_items):
            qty = item.get('quantity') or 1.0
            unit_price = item.get('unit_price')
            amount = item.get('amount')
            if amount is None and unit_price is not None:
                amount = qty * unit_price
            item_lines.append(
                f'[{idx}] description="{item.get("description") or ""}", '
                f'quantity={qty}, unit_price={unit_price}, amount={amount}'
            )

        payload = {
            'model': config['model'],
            'input': [{
                'role': 'user',
                'content': [{
                    'type': 'input_text',
                    'text': '\n'.join(prompt_lines + [
                        '',
                        'Allowed accounts:',
                        *candidate_lines,
                        '',
                        'Bill line items:',
                        *item_lines,
                    ]),
                }],
            }],
            'text': {
                'format': {
                    'type': 'json_schema',
                    'name': CLASSIFICATION_SCHEMA['name'],
                    'strict': CLASSIFICATION_SCHEMA['strict'],
                    'schema': CLASSIFICATION_SCHEMA['schema'],
                }
            }
        }
        result = self._openai_responses_request(payload, config['api_key'], timeout=90)
        return result.get('mappings') or []

    def _classify_line_accounts(self, line_items, config):
        fallback_account = self.env['account.account'].browse(config['default_account_id']) if config['default_account_id'] else False
        policy_accounts = self._build_policy_accounts(fallback_account)
        if not line_items:
            return {}

        heuristic_mappings = self._heuristic_account_mapping(
            line_items,
            policy_accounts,
            config['asset_threshold'],
        )
        mapping_by_index = {item['index']: item for item in heuristic_mappings}

        try:
            ai_mappings = self._call_openai_account_classification(line_items, policy_accounts, config)
            valid_codes = {item['code']: item for item in policy_accounts['candidates']}
            for item in ai_mappings:
                code = item.get('account_code')
                index = item.get('index')
                if isinstance(index, int) and code in valid_codes:
                    mapping_by_index[index] = item
        except Exception as exc:
            _logger.warning('Account classification fell back to heuristic mapping: %s', exc)

        requested_codes = [m['account_code'] for m in mapping_by_index.values() if m.get('account_code')]
        account_domain = [('code', 'in', requested_codes)]
        account_model = self.env['account.account']
        if 'company_ids' in account_model._fields:
            account_domain = ['&', ('code', 'in', requested_codes), ('company_ids', 'in', [self.env.company.id])]
        elif 'company_id' in account_model._fields:
            account_domain = ['&', ('code', 'in', requested_codes), '|', ('company_id', '=', False), ('company_id', '=', self.env.company.id)]

        code_to_account = {
            account.code: account
            for account in account_model.search(account_domain)
        }

        resolved = {}
        for idx in range(len(line_items)):
            mapping = mapping_by_index.get(idx)
            account = code_to_account.get(mapping.get('account_code')) if mapping else False
            if not account:
                account = policy_accounts['fallback']
            resolved[idx] = {
                'account': account,
                'reason': mapping.get('reason') if mapping else _('Fallback account used.'),
            }
        return resolved

    def action_create_vendor_bill(self):
        self.ensure_one()
        if self.bill_id:
            return self._open_bill_action()
        if self.state != 'analyzed':
            raise UserError(_('Analyze the document before creating a vendor bill.'))

        config = self._get_config()
        if not config['default_account_id']:
            raise UserError(_('Set a default expense account in Settings first.'))
        if not config['default_journal_id']:
            raise UserError(_('Set a default purchase journal in Settings first.'))

        data = json.loads(self.extracted_json or '{}')
        line_items = data.get('line_items') or []
        classified_accounts = self._classify_line_accounts(line_items, config)

        invoice_lines = []
        for idx, item in enumerate(line_items):
            qty = item.get('quantity') or 1.0
            price_unit = item.get('unit_price')
            amount = item.get('amount')
            if price_unit is None and amount is not None and qty:
                price_unit = amount / qty
            if price_unit is None:
                price_unit = 0.0

            resolved = classified_accounts.get(idx, {})
            account = resolved.get('account')
            if not account:
                account = self.env['account.account'].browse(config['default_account_id'])

            line_name = item.get('description') or 'Receipt line'
            reason = resolved.get('reason')
            if reason:
                line_name = f'{line_name}\n[AI Account Match] {reason}'

            invoice_lines.append((0, 0, {
                'name': line_name,
                'quantity': qty,
                'price_unit': price_unit,
                'account_id': account.id,
            }))

        if not invoice_lines:
            invoice_lines = [(0, 0, {
                'name': self.extraction_summary or self.file_name,
                'quantity': 1.0,
                'price_unit': self.total_amount or 0.0,
                'account_id': config['default_account_id'],
            })]

        bill_vals = {
            'move_type': 'in_invoice',
            'partner_id': self.partner_id.id if self.partner_id else False,
            'journal_id': config['default_journal_id'],
            'invoice_date': self.invoice_date or False,
            'invoice_date_due': self.due_date or False,
            'ref': self.invoice_number or self.payment_reference or self.file_name,
            'currency_id': self.currency_id.id if self.currency_id else self.env.company.currency_id.id,
            'invoice_line_ids': invoice_lines,
        }
        bill = self.env['account.move'].create(bill_vals)

        attachment = self.env['ir.attachment'].create({
            'name': self.file_name,
            'datas': self.file_data,
            'res_model': 'account.move',
            'res_id': bill.id,
            'mimetype': self.mime_type,
        })
        _logger.info('Attached source file %s to bill %s as attachment %s', self.file_name, bill.name, attachment.id)

        self.write({'bill_id': bill.id, 'state': 'done'})
        return self._open_bill_action()

    def _open_bill_action(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Vendor Bill'),
            'res_model': 'account.move',
            'res_id': self.bill_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
            'target': 'current',
        }
