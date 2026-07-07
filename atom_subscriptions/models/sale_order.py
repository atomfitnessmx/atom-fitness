from odoo import models, fields, api
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    tc_pactado = fields.Float(
        string='TC Pactado (USD/MXN)',
        digits=(16, 4),
        help='Tipo de cambio acordado con el cliente al firmar el contrato.',
    )

    meses_renta = fields.Integer(
        string='Plazo (meses)',
        default=0,
        help='Número de meses del contrato de renta.',
    )

    # Campo no computado — se calcula on-the-fly en onchange y se fija al confirmar
    cuota_mensual_mxn = fields.Float(
        string='Cuota Mensual MXN $',
        digits=(16, 2),
        readonly=True,
        store=True,
        help='Cuota mensual en MXN = Total USD × TC Pactado ÷ Meses.',
    )

    asset_ids = fields.Many2many(
        comodel_name='account.asset',
        relation='sale_order_asset_rel',
        column1='order_id',
        column2='asset_id',
        string='Activos Fijos',
    )

    asset_count = fields.Integer(
        string='# Activos Fijos',
        compute='_compute_asset_count',
    )

    @api.depends('asset_ids')
    def _compute_asset_count(self):
        for order in self:
            order.asset_count = len(order.asset_ids)

    def _calcular_cuota_mxn(self):
        """Calcula la cuota mensual en MXN según moneda, TC y meses."""
        self.ensure_one()
        meses = self.meses_renta
        if meses <= 0:
            return 0.0
        total_recurrente = sum(
            line.price_subtotal
            for line in self.order_line
            if line.product_id.recurring_invoice
        )
        if total_recurrente <= 0:
            return 0.0
        if self.currency_id.name == 'USD' and self.tc_pactado > 0:
            return (total_recurrente * self.tc_pactado) / meses
        elif self.currency_id.name == 'MXN':
            return total_recurrente / meses
        return 0.0

    @api.onchange('tc_pactado', 'meses_renta', 'currency_id', 'order_line')
    def _onchange_renta_fields(self):
        """Actualiza la cuota en tiempo real mientras edita la cotización."""
        for order in self:
            order.cuota_mensual_mxn = order._calcular_cuota_mxn()
            if order.meses_renta > 0 and order.currency_id.name == 'USD' and not order.tc_pactado:
                return {
                    'warning': {
                        'title': 'TC Pactado requerido',
                        'message': 'La cotización está en USD. '
                                   'Ingresa el TC pactado para calcular la cuota mensual en MXN.',
                    }
                }

    def action_confirm(self):
        for order in self:
            if not order.meses_renta or order.meses_renta <= 0:
                continue

            meses = order.meses_renta

            # Calcular y fijar cuota ANTES de modificar precios
            cuota_fija = order._calcular_cuota_mxn()

            if order.currency_id.name == 'USD':
                if not order.tc_pactado or order.tc_pactado <= 0:
                    raise UserError(
                        f'La cotización {order.name} está en USD con plazo de {meses} meses.\n\n'
                        f'Debes ingresar el TC Pactado antes de confirmar.'
                    )
                lineas_recurrentes = order.order_line.filtered(
                    lambda l: l.product_id.recurring_invoice
                )
                lineas_no_recurrentes = order.order_line.filtered(
                    lambda l: not l.product_id.recurring_invoice
                )
                if not lineas_recurrentes:
                    raise UserError(
                        f'La cotización {order.name} tiene plazo de renta pero ningún '
                        f'producto está configurado como recurrente.'
                    )
                for line in lineas_recurrentes:
                    precio_usd = line.price_unit
                    line.write({
                        'precio_venta_usd': precio_usd,
                        'price_unit': (precio_usd * order.tc_pactado) / meses,
                    })
                for line in lineas_no_recurrentes:
                    if line.price_unit > 0:
                        line.write({
                            'price_unit': line.price_unit * order.tc_pactado,
                        })
                pricelist_mxn = self.env['product.pricelist'].search(
                    [('currency_id.name', '=', 'MXN')], limit=1
                )
                if pricelist_mxn:
                    order.write({'pricelist_id': pricelist_mxn.id})

            else:
                lineas_recurrentes = order.order_line.filtered(
                    lambda l: l.product_id.recurring_invoice
                )
                for line in lineas_recurrentes:
                    line.write({'price_unit': line.price_unit / meses})

            if not order.plan_id:
                plan_renta = self.env['sale.subscription.plan'].search(
                    [('name', '=', 'Renta Anual')], limit=1
                )
                if plan_renta:
                    order.write({'plan_id': plan_renta.id})

            # Fijar cuota calculada ANTES de confirmar (moneda era USD)
            order.write({'cuota_mensual_mxn': cuota_fija})

        result = super().action_confirm()

        # Crear entregas después de confirmar
        for order in self:
            if order.meses_renta and order.meses_renta > 0:
                lineas_recurrentes = order.order_line.filtered(
                    lambda l: l.product_id.recurring_invoice
                )
                self._crear_entrega_renta(order, lineas_recurrentes)

        return result

    def _crear_entrega_renta(self, order, lineas_recurrentes):
        """Crea orden de entrega vinculada a la OV para productos recurrentes."""
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'outgoing'),
            ('warehouse_id.lot_stock_id.complete_name', 'ilike', 'WH-NA'),
        ], limit=1)

        if not picking_type:
            return

        ubicacion_destino = self.env['stock.location'].search([
            ('usage', '=', 'customer'),
        ], limit=1)

        moves = []
        for line in lineas_recurrentes:
            if line.product_id.type == 'product':
                moves.append((0, 0, {
                    'name': line.product_id.name,
                    'product_id': line.product_id.id,
                    'product_uom_qty': line.product_uom_qty,
                    'product_uom': line.product_uom.id,
                    'location_id': picking_type.default_location_src_id.id,
                    'location_dest_id': ubicacion_destino.id,
                    'sale_line_id': line.id,
                }))

        if not moves:
            return

        picking = self.env['stock.picking'].create({
            'partner_id': order.partner_id.id,
            'picking_type_id': picking_type.id,
            'location_id': picking_type.default_location_src_id.id,
            'location_dest_id': ubicacion_destino.id,
            'origin': order.name,
            'move_ids': moves,
        })

        if order.procurement_group_id:
            picking.write({'group_id': order.procurement_group_id.id})

        return picking

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
        string='Precio Venta USD',
        digits=(16, 2),
        readonly=True,
        help='Precio original en USD antes de conversión a cuota mensual MXN.',
    )
