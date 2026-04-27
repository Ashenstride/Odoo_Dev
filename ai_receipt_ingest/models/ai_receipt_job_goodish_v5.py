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
                    {
                        'type': 'input_file',
                        'file_id': file_id,
                    },
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

        req = request.Request(
            'https://api.openai.com/v1/responses',
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f"Bearer {config['api_key']}",
            },
            method='POST',
        )

        try:
            with request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode('utf-8'))
        except error.HTTPError as exc:
            raw = exc.read().decode('utf-8', errors='ignore')
            _logger.exception('OpenAI extraction HTTP error: %s', raw)
            raise UserError(_('OpenAI extraction failed: %s') % raw)
        except Exception as exc:
            _logger.exception('OpenAI extraction failed')
            raise UserError(_('OpenAI extraction failed: %s') % exc)

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
        invoice_lines = []
        for item in line_items:
            qty = item.get('quantity') or 1.0
            price_unit = item.get('unit_price')
            amount = item.get('amount')
            if price_unit is None and amount is not None and qty:
                price_unit = amount / qty
            if price_unit is None:
                price_unit = 0.0
            invoice_lines.append((0, 0, {
                'name': item.get('description') or 'Receipt line',
                'quantity': qty,
                'price_unit': price_unit,
                'account_id': config['default_account_id'],
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
