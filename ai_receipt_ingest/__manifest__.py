{
    'name': 'AI Receipt Ingest',
    'version': '19.0.2.1.0',
    'summary': 'Upload a receipt, extract structured fields with OpenAI, and create a draft vendor bill.',
    'author': 'OpenAI / Peter scaffold',
    'license': 'LGPL-3',
    'depends': ['base', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'views/ai_receipt_job_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'application': True,
    'installable': True,
}
