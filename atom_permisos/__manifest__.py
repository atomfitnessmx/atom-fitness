{
    'name': 'Atom Fitness - Permisos y Seguridad',
    'version': '17.0.1.0.0',
    'category': 'Human Resources/Employees',
    'summary': 'Ocultar costos de producto a Ventas/Inventario y regla de visibilidad de leads (ver todos, editar solo los propios)',
    'author': 'RDA Advisory',
    'website': 'https://rdaadvisory.com',
    'license': 'LGPL-3',
    'depends': [
        'product',
        'crm',
        'sale',
    ],
    'data': [
        'security/security_groups.xml',
        'security/crm_lead_rules.xml',
        'views/product_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
