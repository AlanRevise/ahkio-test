# noinspection PyStatementEffect
{
    'name': 'Import/Export invoices with Finvoice 3.0',
    'summary': 'Import/Export invoices with Finvoice 3.0',
    'description': 'Import/Export invoices with Finvoice 3.0',
    'author': 'Ahkio Consulting Oy',
    'website': 'https://www.ahkio.com/',
    'license': 'Other proprietary',
    'category': 'Accounting/Accounting',
    'version': '0.2',
    'depends': ['account_edi', 'ahkio_account_base'],
    'data': [
        'security/ir.model.access.csv',
        'data/account_edi_data.xml',
        'data/config_parameter.xml',
        'data/cron.xml',
        'data/finvoice_templates.xml',
        'views/res_company_views.xml',
        'views/res_partner_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
