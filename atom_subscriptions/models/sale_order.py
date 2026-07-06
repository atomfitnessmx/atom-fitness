from odoo import models, fields, api
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # TC pactado con el cliente al firmar
    tc_pactado = fields.Float(
        string='TC Pactado (USD/MXN)',
        digits=(16, 4),
        help='Tipo de cambio acordado con el cliente al firmar. '
             'Se usa para calcular la cuota mensual fija en MXN.',
    )

    # Meses libres — el usuario escribe el plazo directamente
    meses_renta = fields.Integer(
        string='Plazo (meses)',
        default=0,
        help='Número de meses del contrato de renta. '
             'Ejemplo: 36, 48, 60. Se aplica al crear la suscripción.',
    )

    # Cuota mensual calculada (informativa)
    cuota_mensual_mxn = fields.Monetary(
        string='Cuota Mensual MXN',
        currency_field='currency_id',
        compute='_compute_cuota_mensual',
        store=True,
        help='Total MXN ÷ meses de renta. Referencia antes de confirmar.',
    )

    @api.depends('amount_total', 'tc_pactado', 'currency_id', 'meses_renta')
    def _compute_cuota_mensual(self):
        for order in self:
            cuota = 0.0
            meses = order.meses_renta

            if meses > 0 and order.amount_total > 0:
                if order.currency_id.name == 'USD' and order.tc_pactado > 0:
                    total_mxn = order.amount_total * order.tc_pactado
                    cuota = total_mxn / meses
                elif order.currency_id.name == 'MXN':
                    cuota = order.amount_total / meses

            order.cuota_mensual_mxn = cuota

    @api.onchange('tc_pactado', 'meses_renta', 'currency_id')
    def _onchange_renta_fields(self):
        """Avisos de validación en tiempo real."""
        if self.meses_renta > 0:
            if self.currency_id.name == 'USD' and not self.tc_pactado:
                return {
                    'warning': {
                        'title': 'TC Pactado requerido',
                        'message': 'La cotización está en USD. '
                                   'Ingresa el TC pactado para calcular la cuota mensual en MXN.',
                    }
                }
            if self.meses_renta < 1 or self.meses_renta > 120:
                return {
                    'warning': {
                        'title': 'Plazo inusual',
                        'message': f'El plazo de {self.meses_renta} meses parece inusual. '
                                   'Verifica antes de continuar.',
                    }
                }

    def action_confirm(self):
        """Al confirmar: validar datos, convertir precios a cuota mensual MXN."""
        for order in self:
            # Solo procesar si tiene meses de renta definidos
            if not order.meses_renta or order.meses_renta <= 0:
                continue

            meses = order.meses_renta

            # Validar TC si es en USD
            if order.currency_id.name == 'USD':
                if not order.tc_pactado or order.tc_pactado <= 0:
                    raise UserError(
                        f'La cotización {order.name} está en USD con plazo de renta de '
                        f'{meses} meses.\n\n'
                        f'Debes ingresar el TC Pactado antes de confirmar.'
                    )

                # Guardar precio original USD y calcular cuota MXN por línea
                for line in order.order_line:
                    if line.product_id.recurring_invoice:
                        precio_usd = line.price_unit
                        # Guardar referencia del precio USD original
                        line.write({
                            'precio_venta_usd': precio_usd,
                            'price_unit': (precio_usd * order.tc_pactado) / meses,
                        })

                # Cambiar pricelist a MXN
                pricelist_mxn = self.env['product.pricelist'].search(
                    [('currency_id.name', '=', 'MXN')], limit=1
                )
                if pricelist_mxn:
                    order.write({'pricelist_id': pricelist_mxn.id})

            else:
                # Cotización en MXN → solo dividir entre meses
                for line in order.order_line:
                    if line.product_id.recurring_invoice:
                        line.write({
                            'price_unit': line.price_unit / meses,
                        })

            # Asignar plan base de renta si no tiene uno
            if not order.plan_id:
                plan_renta = self.env['sale.subscription.plan'].search(
                    [('name', '=', 'Renta Anual')], limit=1
                )
                if plan_renta:
                    order.write({'plan_id': plan_renta.id})

            # Ajustar auto_close_limit del plan al número de meses
            if order.plan_id:
                order.plan_id.write({'auto_close_limit': meses})

        return super().action_confirm()


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    # Guarda el precio USD original antes de convertir a cuota MXN
    precio_venta_usd = fields.Float(
        string='Precio Venta USD',
        digits=(16, 2),
        readonly=True,
        help='Precio original en USD antes de conversión a cuota mensual MXN.',
    )
