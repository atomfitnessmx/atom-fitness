{
    'name': 'Atom Fitness - Suscripciones Renta',
    'version': '17.0.2.1.0',
    'category': 'Sales/Subscriptions',
    'summary': 'Conversión de cotización a arrendamiento con cuota mensual y capitalización automática de AF',
    'author': 'RDA Advisory',
    'website': 'https://rdaadvisory.com',
    'license': 'LGPL-3',
    'depends': [
        'sale',
        'sale_subscription',
        'product',
        'stock',
        'account_asset',
    ],
    'data': [
        'views/sale_order_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
