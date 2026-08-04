from odoo import models, fields


class ProductCategory(models.Model):
    _inherit = 'product.category'

    es_categoria_rentable = fields.Boolean(
        string='Categoría Rentable',
        default=False,
        help='Marca esta categoría si sus productos son equipo físico que puede '
             'convertirse a contrato de arrendamiento desde la cotización.',
    )
