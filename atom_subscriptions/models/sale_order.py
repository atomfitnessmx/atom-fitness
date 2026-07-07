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

    cuota_mensual_mxn = fields.Float(
        string='Cuota Mensual MXN',
        digits=(16, 2),
        compute='_compute_cuota_mensual',
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

    @api.depends('order_line.price_subtotal', 'order_line.product_id.recurring_invoice',
                 'tc_pactado', 'currency_id', 'meses_renta')
    def _compute_cuota_mensual(self):
        for order in self:
            cuota = 0.0
            meses = order.meses_renta
            if meses > 0:
                total_recurrente = sum(
                    line.price_subtotal
                    for line in order.order_line
                    if line.product_id.recurring_invoice
                )
                if total_recurrente > 0:
                    if order.currency_id.name == 'USD' and order.tc_pactado > 0:
                        # USD × TC ÷ meses = MXN por mes
                        cuota = (total_recurrente * order.tc_pactado) / meses
                    elif order.currency_id.name == 'MXN':
                        cuota = total_recurrente / meses
            order.cuota_mensual_mxn = cuota

    @api.onchange('tc_pactado', 'meses_renta', 'currency_id')
    def _onchange_renta_fields(self):
        if self.meses_renta > 0:
            if self.currency_id.name == 'USD' and not self.tc_pactado:
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

                # Crear orden de entrega vinculada a la OV
                self._crear_entrega_renta(order, lineas_recurrentes)

            else:
                lineas_recurrentes = order.order_line.filtered(
                    lambda l: l.product_id.recurring_invoice
                )
                for line in lineas_recurrentes:
                    line.write({'price_unit': line.price_unit / meses})
                self._crear_entrega_renta(order, lineas_recurrentes)

            if not order.plan_id:
                plan_renta = self.env['sale.subscription.plan'].search(
                    [('name', '=', 'Renta Anual')], limit=1
                )
                if plan_renta:
                    order.write({'plan_id': plan_renta.id})

        return super().action_confirm()

    def _crear_entrega_renta(self, order, lineas_recurrentes):
        """Crea una orden de entrega vinculada a la OV para los productos recurrentes."""
        # Buscar tipo de operación de entrega del almacén principal
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

        # Vincular a la OV via procurement_group
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
