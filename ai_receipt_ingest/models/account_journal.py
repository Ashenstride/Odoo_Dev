import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountJournal(models.Model):
    _inherit = 'account.journal'

    _AI_SUPPORTED_MIME_PREFIXES = ('image/',)
    _AI_SUPPORTED_MIME_TYPES = {
        'application/pdf',
    }
    _AI_SUPPORTED_EXTENSIONS = {
        '.pdf', '.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff',
    }

    def _ai_receipt_is_supported_attachment(self, attachment):
        mimetype = (attachment.mimetype or '').lower()
        name = (attachment.name or '').lower()
        return (
            mimetype in self._AI_SUPPORTED_MIME_TYPES
            or mimetype.startswith(self._AI_SUPPORTED_MIME_PREFIXES)
            or any(name.endswith(ext) for ext in self._AI_SUPPORTED_EXTENSIONS)
        )

    def _ai_receipt_hook_config(self):
        icp = self.env['ir.config_parameter'].sudo()
        api_key = icp.get_param('ai_receipt_ingest.openai_api_key')
        return {
            'enabled': bool(api_key),
            'strict': False,
            'api_key': api_key,
        }

    def _ai_receipt_process_attachments(self, attachments):
        self.ensure_one()
        jobs = self.env['ai.receipt.job']
        created_bills = self.env['account.move']
        for attachment in attachments:
            if not attachment.datas:
                raise UserError(_('Attachment %s has no binary data to analyze.') % (attachment.name or attachment.id))
            job = jobs.create({
                'name': attachment.name or _('Uploaded Receipt'),
                'file_name': attachment.name or 'upload',
                'file_data': attachment.datas,
            })
            job.action_analyze()
            job.action_create_vendor_bill()
            created_bills |= job.bill_id
        return created_bills

    def create_document_from_attachment(self, attachment_ids):
        self.ensure_one()
        config = self._ai_receipt_hook_config()
        if not config['enabled']:
            return super().create_document_from_attachment(attachment_ids)

        attachments = self.env['ir.attachment'].browse(attachment_ids).exists()
        if not attachments:
            return super().create_document_from_attachment(attachment_ids)

        supported = attachments.filtered(self._ai_receipt_is_supported_attachment)
        unsupported = attachments - supported

        if unsupported:
            _logger.info('AI receipt hook bypassed unsupported attachments: %s', unsupported.ids)
            return super().create_document_from_attachment(attachment_ids)

        if not supported:
            return super().create_document_from_attachment(attachment_ids)

        if not config['api_key']:
            if config['strict']:
                raise UserError(_('AI receipt replacement is enabled, but no OpenAI API key is configured.'))
            return super().create_document_from_attachment(attachment_ids)

        try:
            bills = self._ai_receipt_process_attachments(supported)
        except Exception as exc:
            _logger.exception('AI receipt replacement failed during upload flow.')
            if config['strict']:
                if isinstance(exc, UserError):
                    raise
                raise UserError(_('AI receipt replacement failed: %s') % exc)
            return super().create_document_from_attachment(attachment_ids)

        if len(bills) == 1:
            bill = bills[0]
            return {
                'name': _('Vendor Bill'),
                'type': 'ir.actions.act_window',
                'res_model': 'account.move',
                'res_id': bill.id,
                'view_mode': 'form',
                'views': [(False, 'form')],
                'target': 'current',
            }

        return {
            'name': _('Vendor Bills'),
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('id', 'in', bills.ids)],
            'target': 'current',
        }
