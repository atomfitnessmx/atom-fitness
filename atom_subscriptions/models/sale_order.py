from odoo import models, fields, api
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    tc_pactado = fields.Float(
        string='TC Pactado (USD/MXN)',
        digits=(16, 4),
        help='Tipo de cambio acordado con el cliente al firmar el contrato de arrendamiento.',
    )

    meses_renta = fields.Integer(
        string='Plazo (meses)',
        default=0,
        help='Número de meses del contrato de arrendamiento.',
    )

    cuota_mensual_mxn = fields.Float(
        string='Cuota Mensual MXN $ (antes de IVA)',
        digits=(16, 2),
        readonly=True,
        store=True,
        compute='_compute_cuota_mensual',
        help='Cuota mensual en MXN, antes de IVA = Total USD del equipo x TC Pactado / Meses.',
    )

    cuota_fijada = fields.Boolean(string='Cuota Fijada', default=False, copy=False)

    es_arrendamiento = fields.Boolean(
        string='Es Arrendamiento', default=False, copy=False,
        help='Indica que esta cotización fue convertida a contrato de arrendamiento.',
    )

    tiene_producto_rentable = fields.Boolean(
        string='Tiene Producto Rentable',
        compute='_compute_tiene_producto_rentable',
    )

    # ------- Información del contrato -------
    nombre_contrato = fields.Char(
        string='Nombre del Contrato',
        copy=False,
        help='Nombre con el que se reconocerá el contrato de arrendamiento. '
             'Si se deja vacío, al confirmar se usa el folio de la orden. '
             'Con este nombre se crea la cuenta analítica del contrato.',
    )

    valor_contrato_usd = fields.Float(
        string='Valor del Contrato USD',
        digits=(16, 2), readonly=True, copy=False,
        help='Valor total del equipo del contrato en USD (fijado al convertir).',
    )

    valor_contrato_mxn = fields.Float(
        string='Valor del Contrato MXN',
        digits=(16, 2), readonly=True, copy=False,
        help='Valor total del contrato en MXN = USD x TC Pactado (fijado al confirmar).',
    )

    cuenta_analitica_id = fields.Many2one(
        'account.analytic.account',
        string='Cuenta Analítica del Contrato',
        readonly=True, copy=False,
        help='Cuenta analítica del plan "Renta" donde se registran ingresos, '
             'inversión y depreciación de este contrato.',
    )

    asset_ids = fields.Many2many(
        comodel_name='account.asset',
        relation='sale_order_asset_rel',
        column1='order_id', column2='asset_id',
        string='Activos Fijos',
    )

    asset_count = fields.Integer(string='# Activos Fijos', compute='_compute_asset_count')

    @api.depends('asset_ids')
    def _compute_asset_count(self):
        for order in self:
            order.asset_count = len(order.asset_ids)

    @api.depends('order_line.product_id.categ_id.es_categoria_rentable')
    def _compute_tiene_producto_rentable(self):
        for order in self:
            order.tiene_producto_rentable = any(
                line.product_id.categ_id.es_categoria_rentable
                for line in order.order_line
                if not line.display_type
            )

    @api.depends('order_line.price_unit', 'order_line.product_uom_qty',
                 'order_line.product_id.default_code',
                 'tc_pactado', 'currency_id', 'meses_renta', 'cuota_fijada')
    def _compute_cuota_mensual(self):
        for order in self:
            if order.cuota_fijada:
                continue
            meses = order.meses_renta
            if meses <= 0:
                order.cuota_mensual_mxn = 0.0
                continue
            total_equipo = sum(
                line.price_unit * line.product_uom_qty
                for line in order.order_line
                if line.product_id.default_code == 'ARR-GYM-MENSUAL'
            )
            if total_equipo <= 0:
                order.cuota_mensual_mxn = 0.0
                continue
            if order.currency_id.name == 'USD' and order.tc_pactado > 0:
                order.cuota_mensual_mxn = (total_equipo * order.tc_pactado) / meses
            elif order.currency_id.name == 'MXN':
                order.cuota_mensual_mxn = total_equipo / meses
            else:
                order.cuota_mensual_mxn = 0.0

    @api.onchange('tc_pactado', 'meses_renta', 'currency_id')
    def _onchange_renta_fields(self):
        if self.meses_renta > 0 and self.currency_id.name == 'USD' and not self.tc_pactado:
            return {
                'warning': {
                    'title': 'TC Pactado requerido',
                    'message': 'La cotización está en USD. '
                               'Ingresa el TC pactado para calcular la cuota mensual en MXN.',
                }
            }

    def action_convertir_arrendamiento(self):
        self.ensure_one()

        if self.state not in ('draft', 'sent'):
            raise UserError(
                'Solo se puede convertir a arrendamiento una cotización en '
                'estado Borrador o Cotización enviada.'
            )
        if self.es_arrendamiento:
            raise UserError('Esta cotización ya fue convertida a arrendamiento.')

        lineas_rentables = self.order_line.filtered(
            lambda l: l.product_id.categ_id.es_categoria_rentable and not l.display_type
        )
        if not lineas_rentables:
            raise UserError(
                'No hay productos de categorías rentables en esta cotización. '
                'Verifica que el producto cotizado pertenezca a una categoría '
                'marcada como rentable.'
            )

        total_equipo = sum(
            line.price_unit * line.product_uom_qty for line in lineas_rentables
        )

        self.write({
            'es_arrendamiento': True,
            'valor_contrato_usd': total_equipo,
        })

        for line in lineas_rentables:
            line.write({
                'precio_venta_usd': line.price_unit,
                'price_unit': 0.0,
            })

        producto_renta = self.env['product.template'].search([
            ('default_code', '=', 'ARR-GYM-MENSUAL')
        ], limit=1)
        if not producto_renta:
            raise UserError(
                'No se encontró el producto "Servicio de arrendamiento operativo '
                'mensual de equipo de gimnasio" (código ARR-GYM-MENSUAL). '
                'Contacta a soporte antes de continuar.'
            )

        self.env['sale.order.line'].create({
            'order_id': self.id,
            'product_id': producto_renta.product_variant_id.id,
            'product_uom_qty': 1,
            'price_unit': total_equipo,
            'name': producto_renta.name,
        })

        plan_renta = self.env['sale.subscription.plan'].search(
            [('name', '=', 'Renta')], limit=1
        )
        self.write({'plan_id': plan_renta.id if plan_renta else False})

    def _crear_cuenta_analitica_contrato(self):
        """Crea (una sola vez) la cuenta analítica del contrato dentro del
        plan analítico 'Renta' y la devuelve."""
        self.ensure_one()
        if self.cuenta_analitica_id:
            return self.cuenta_analitica_id

        plan = self.env['account.analytic.plan'].search([('name', '=', 'Renta')], limit=1)
        if not plan:
            plan = self.env['account.analytic.plan'].create({'name': 'Renta'})

        nombre = self.nombre_contrato or self.name
        cuenta = self.env['account.analytic.account'].create({
            'name': nombre,
            'plan_id': plan.id,
            'partner_id': self.partner_id.id,
        })
        self.write({'cuenta_analitica_id': cuenta.id})
        return cuenta

    def action_confirm(self):
        for order in self:
            if not order.es_arrendamiento or not order.meses_renta or order.meses_renta <= 0:
                continue

            meses = order.meses_renta

            linea_servicio = order.order_line.filtered(
                lambda l: l.product_id.default_code == 'ARR-GYM-MENSUAL'
            )
            if not linea_servicio:
                raise UserError(
                    f'La cotización {order.name} está marcada como arrendamiento '
                    f'pero no tiene la línea del servicio de renta (ARR-GYM-MENSUAL). '
                    f'Usa el botón "Convertir a Arrendamiento" antes de confirmar.'
                )
            if len(linea_servicio) > 1:
                raise UserError(
                    f'La cotización {order.name} tiene más de una línea del servicio '
                    f'de arrendamiento. Debe existir solo una.'
                )

            total_equipo = linea_servicio.price_unit

            if order.currency_id.name == 'USD':
                if not order.tc_pactado or order.tc_pactado <= 0:
                    raise UserError(
                        f'La cotización {order.name} está en USD con plazo de {meses} meses.\n\n'
                        f'Debes ingresar el TC Pactado antes de confirmar.'
                    )
                cuota = (total_equipo * order.tc_pactado) / meses
                valor_mxn = total_equipo * order.tc_pactado
                pricelist_mxn = self.env['product.pricelist'].search(
                    [('currency_id.name', '=', 'MXN')], limit=1
                )
                if pricelist_mxn:
                    order.write({'pricelist_id': pricelist_mxn.id})
            else:
                cuota = total_equipo / meses
                valor_mxn = total_equipo

            # Cuenta analítica del contrato + distribución en la línea de renta
            cuenta = order._crear_cuenta_analitica_contrato()
            linea_servicio.write({
                'price_unit': cuota,
                'analytic_distribution': {str(cuenta.id): 100},
            })

            order.write({
                'cuota_mensual_mxn': cuota,
                'valor_contrato_mxn': valor_mxn,
                'cuota_fijada': True,
            })

        return super().action_confirm()

    def action_view_assets(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Activos Fijos',
            'res_model': 'account.asset',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.asset_ids.ids)],
            'context': {'default_partner_id': self.partner_id.id},
        }


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    precio_venta_usd = fields.Float(
        string='Precio Cotizado USD',
        digits=(16, 2), readonly=True, copy=False,
        help='Precio original cotizado en USD antes de la conversión a arrendamiento.',
    )

    def _compute_price_unit(self):
        lineas_arrendamiento = self.filtered(lambda l: l.order_id.es_arrendamiento)
        lineas_normales = self - lineas_arrendamiento
        if lineas_normales:
            super(SaleOrderLine, lineas_normales)._compute_price_unit()
