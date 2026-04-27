from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ai_receipt_openai_api_key = fields.Char(
        string='OpenAI API Key',
        config_parameter='ai_receipt_ingest.openai_api_key'
    )
    ai_receipt_openai_model = fields.Char(
        string='OpenAI Model',
        config_parameter='ai_receipt_ingest.openai_model',
        default='gpt-4.1-mini'
    )
    ai_receipt_default_expense_account_id = fields.Many2one(
        'account.account',
        string='Default Expense Account',
        config_parameter='ai_receipt_ingest.default_expense_account_id'
    )
    ai_receipt_default_purchase_journal_id = fields.Many2one(
        'account.journal',
        string='Default Purchase Journal',
        domain=[('type', '=', 'purchase')],
        config_parameter='ai_receipt_ingest.default_purchase_journal_id'
    )
    ai_receipt_auto_create_partner = fields.Boolean(
        string='Auto-create Vendor If Missing',
        config_parameter='ai_receipt_ingest.auto_create_partner',
        default=True,
    )
