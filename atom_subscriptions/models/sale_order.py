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
        help='Cuota mensual fija en MXN, antes de IVA = Total USD del equipo x TC Pactado / Meses.',
    )

    cuota_fijada = fields.Boolean(
        string='Cuota Fijada',
        default=False,
        copy=False,
    )

    es_arrendamiento = fields.Boolean(
        string='Es Arrendamiento',
        default=False,
        copy=False,
        help='Indica que esta cotización fue convertida a contrato de arrendamiento.',
    )

    tiene_producto_rentable = fields.Boolean(
        string='Tiene Producto Rentable',
        compute='_compute_tiene_producto_rentable',
        help='True si al menos una línea tiene un producto de categoría marcada como rentable.',
    )

    asset_ids = fields.Many2many(
        comodel_name='account.asset',
        relation='sale_order_asset_rel',
        column1='order_id',
        column2='asset_id',
        string='Activos Fijos',
        help='Activos fijos capitalizados asociados a este contrato de arrendamiento.',
    )

    asset_count = fields.Integer(
        string='# Activos Fijos',
        compute='_compute_asset_count',
    )

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

    @api.depends('order_line.price_subtotal', 'order_line.product_id.default_code',
                 'tc_pactado', 'currency_id', 'meses_renta', 'cuota_fijada')
    def _compute_cuota_mensual(self):
        # Usamos price_subtotal (siempre neto, sin IVA) como base — nunca
        # price_unit, cuyo significado depende de si el impuesto tiene
        # "Precio con impuestos incluidos" activado o no.
        for order in self:
            if order.cuota_fijada:
                continue

            meses = order.meses_renta
            if meses <= 0:
                order.cuota_mensual_mxn = 0.0
                continue

            total_neto = sum(
                line.price_subtotal
                for line in order.order_line
                if line.product_id.default_code == 'ARR-GYM-MENSUAL'
            )

            if total_neto <= 0:
                order.cuota_mensual_mxn = 0.0
                continue

            if order.currency_id.name == 'USD' and order.tc_pactado > 0:
                order.cuota_mensual_mxn = (total_neto * order.tc_pactado) / meses
            elif order.currency_id.name == 'MXN':
                order.cuota_mensual_mxn = total_neto / meses
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

    @staticmethod
    def _monto_bruto_desde_neto(monto_neto, taxes):
        """Si las taxes de la línea tienen 'Precio con impuestos incluidos'
        activado, price_unit se interpreta como el monto CON impuesto.
        Esta función 'engorda' un monto neto deseado para que, al guardarlo
        en price_unit, el price_subtotal resultante sea exactamente el
        monto neto que queremos (evita que Odoo lo encoja silenciosamente)."""
        factor = 1.0
        for tax in taxes:
            if tax.price_include:
                factor *= (1 + tax.amount / 100.0)
        return monto_neto * factor

    def action_convertir_arrendamiento(self):
        """Convierte las líneas de equipo rentable en una sola línea del
        servicio de arrendamiento, sumando su valor neto total en USD."""
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

        # Total NETO (sin IVA) de las líneas de equipo — esta es la base
        # real del contrato, independientemente de cómo esté configurado
        # el impuesto en cada línea original.
        total_equipo_neto = sum(lineas_rentables.mapped('price_subtotal'))

        # Marcar como arrendamiento ANTES de tocar las líneas, para que
        # nuestra protección de precio (ver SaleOrderLine más abajo) esté
        # activa durante todo el resto del proceso.
        self.write({'es_arrendamiento': True})

        for line in lineas_rentables:
            line.write({'price_unit': 0.0})

        producto_renta = self.env['product.template'].search([
            ('default_code', '=', 'ARR-GYM-MENSUAL')
        ], limit=1)
        if not producto_renta:
            raise UserError(
                'No se encontró el producto "Servicio de arrendamiento operativo '
                'mensual de equipo de gimnasio" (código ARR-GYM-MENSUAL). '
                'Contacta a soporte antes de continuar.'
            )

        price_unit_bruto = self._monto_bruto_desde_neto(
            total_equipo_neto, producto_renta.taxes_id
        )

        self.env['sale.order.line'].create({
            'order_id': self.id,
            'product_id': producto_renta.product_variant_id.id,
            'product_uom_qty': 1,
            'price_unit': price_unit_bruto,
            'name': producto_renta.name,
        })

        plan_renta = self.env['sale.subscription.plan'].search(
            [('name', '=', 'Renta')], limit=1
        )
        self.write({'plan_id': plan_renta.id if plan_renta else False})

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

            # Base NETA (sin IVA) del total del equipo, tomada del price_subtotal
            # actual de la línea de servicio — nunca de price_unit directamente.
            total_equipo_neto = linea_servicio.price_subtotal

            if order.currency_id.name == 'USD':
                if not order.tc_pactado or order.tc_pactado <= 0:
                    raise UserError(
                        f'La cotización {order.name} está en USD con plazo de {meses} meses.\n\n'
                        f'Debes ingresar el TC Pactado antes de confirmar.'
                    )
                cuota_neta = (total_equipo_neto * order.tc_pactado) / meses
                pricelist_mxn = self.env['product.pricelist'].search(
                    [('currency_id.name', '=', 'MXN')], limit=1
                )
                if pricelist_mxn:
                    order.write({'pricelist_id': pricelist_mxn.id})
            else:
                cuota_neta = total_equipo_neto / meses

            precio_unit_bruto = order._monto_bruto_desde_neto(
                cuota_neta, linea_servicio.tax_id
            )
            linea_servicio.write({'price_unit': precio_unit_bruto})

            order.write({
                'cuota_mensual_mxn': cuota_neta,
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

    def _compute_price_unit(self):
        """Una vez que una orden queda marcada como arrendamiento, NINGUNA
        de sus líneas se recalcula automáticamente vía el motor de precios
        de Odoo — todas las mantiene fijas nuestro flujo (conversión y
        confirmación). Esto evita que el equipo en $0 o la cuota calculada
        se sobreescriban silenciosamente por triggers internos de Odoo."""
        lineas_arrendamiento = self.filtered(lambda l: l.order_id.es_arrendamiento)
        lineas_normales = self - lineas_arrendamiento

        if lineas_normales:
            super(SaleOrderLine, lineas_normales)._compute_price_unit()
