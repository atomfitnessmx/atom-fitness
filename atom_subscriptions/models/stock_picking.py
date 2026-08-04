from dateutil.relativedelta import relativedelta

from odoo import models, fields


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def button_validate(self):
        res = super().button_validate()
        for picking in self:
            if (
                picking.state == 'done'
                and picking.sale_id
                and picking.sale_id.es_arrendamiento
                and picking.picking_type_code == 'outgoing'
            ):
                picking._crear_activos_arrendamiento()
        return res

    def _crear_activos_arrendamiento(self):
        self.ensure_one()
        order = self.sale_id

        modelo = self.env['account.asset'].search([
            ('state', '=', 'model'),
            ('name', '=', 'Equipo de gimnasio en renta'),
        ], limit=1)

        cuenta_puente = self.env['account.account'].search(
            [('code', '=', '107.05.01')], limit=1
        )

        if not modelo:
            self.message_post(body=(
                'Arrendamiento: no se encontró el Modelo de Activo '
                '"Equipo de gimnasio en renta". No se capitalizó automáticamente; '
                'realiza el alta manual.'
            ))
            return

        cuenta_analitica = order.cuenta_analitica_id or order._crear_cuenta_analitica_contrato()
        distribucion = {str(cuenta_analitica.id): 100} if cuenta_analitica else False

        # Primera depreciación: mes COMPLETO al fin del mes siguiente a la
        # capitalización, sin fracción del mes en curso.
        hoy = fields.Date.context_today(self)
        inicio_depreciacion = hoy.replace(day=1) + relativedelta(months=1)

        activos_creados = self.env['account.asset']
        total_reclasificar = 0.0

        moves_rentables = self.move_ids.filtered(
            lambda m: m.state == 'done'
            and m.product_id.categ_id.es_categoria_rentable
        )

        for move in moves_rentables:
            valor = abs(sum(move.stock_valuation_layer_ids.mapped('value')))
            if not valor:
                valor = move.product_id.standard_price * move.quantity
            if not valor:
                continue

            vals_asset = {
                'name': f"{move.product_id.name} — {order.name}",
                'model_id': modelo.id,
                'original_value': valor,
                'acquisition_date': hoy,
                'prorata_date': inicio_depreciacion,
                'account_asset_id': modelo.account_asset_id.id,
                'account_depreciation_id': modelo.account_depreciation_id.id,
                'account_depreciation_expense_id': modelo.account_depreciation_expense_id.id,
                'journal_id': modelo.journal_id.id,
                'method': modelo.method,
                'method_number': modelo.method_number,
                'method_period': modelo.method_period,
                'prorata_computation_type': 'constant_periods',
            }
            if distribucion:
                vals_asset['analytic_distribution'] = distribucion

            asset = self.env['account.asset'].create(vals_asset)
            asset.validate()
            activos_creados |= asset
            total_reclasificar += valor

        if not activos_creados:
            return

        order.write({'asset_ids': [(4, a.id) for a in activos_creados]})

        # Asiento de reclasificación SIN analítica: la inversión se refleja
        # en la analítica del contrato vía la depreciación mensual.
        if cuenta_puente and total_reclasificar > 0:
            diario_misc = self.env['account.journal'].search(
                [('code', '=', 'MISC'), ('type', '=', 'general')], limit=1
            ) or modelo.journal_id
            asiento = self.env['account.move'].create({
                'journal_id': diario_misc.id,
                'ref': f'Capitalización equipo arrendamiento {order.name} ({self.name})',
                'line_ids': [
                    (0, 0, {
                        'account_id': modelo.account_asset_id.id,
                        'name': f'Capitalización equipo {order.name}',
                        'debit': total_reclasificar,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'account_id': cuenta_puente.id,
                        'name': f'Reclasificación cuenta puente {order.name}',
                        'debit': 0.0,
                        'credit': total_reclasificar,
                    }),
                ],
            })
            asiento.action_post()

            self.message_post(body=(
                f'Arrendamiento {order.name}: {len(activos_creados)} Activo(s) '
                f'Fijo(s) creados y confirmados (primera depreciación: fin de '
                f'{inicio_depreciacion.strftime("%m/%Y")}), asiento {asiento.name} '
                f'publicado, analítica "{cuenta_analitica.name}" en los activos.'
            ))
