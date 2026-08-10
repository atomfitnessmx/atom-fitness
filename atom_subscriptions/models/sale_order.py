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

    porcentaje_renta = fields.Float(
        string='Porcentaje de Renta (%)',
        digits=(16, 2),
        default=3.0,
        help='Porcentaje mensual sobre el valor del equipo (en MXN) que define '
             'la cuota de renta. Editable por contrato; 3% es el valor por defecto.',
    )

    valor_equipo_mxn = fields.Float(
        string='Valor Equipo en MXN (histórico)',
        digits=(16, 2), readonly=True, copy=False,
        help='Valor del equipo convertido a MXN al momento de confirmar '
             '(Valor Contrato USD x TC Pactado). Se conserva como referencia '
             'histórica del cálculo, aunque el TC de mercado cambie después.',
    )

    cuota_mensual_mxn = fields.Float(
        string='Cuota Mensual MXN $ (antes de IVA)',
        digits=(16, 2),
        readonly=True,
        store=True,
        compute='_compute_cuota_mensual',
        help='Cuota mensual en MXN, antes de IVA = Valor Equipo MXN x Porcentaje de Renta.',
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
        help='Valor total del equipo del contrato en USD, antes de IVA '
             '(fijado al convertir a arrendamiento).',
    )

    valor_contrato_mxn = fields.Float(
        string='Valor Total del Contrato MXN',
        digits=(16, 2), readonly=True, copy=False,
        help='Valor total del contrato durante toda su vigencia en MXN '
             '= Cuota Mensual x Plazo (meses). Fijado al confirmar.',
    )

    cuenta_analitica_id = fields.Many2one(
        'account.analytic.account',
        string='Cuenta Analítica del Contrato',
        readonly=True, copy=False,
    )

    cotizacion_origen_id = fields.Many2one(
        'sale.order',
        string='Cotización de Origen',
        copy=False,
        domain="[('id', '!=', id), ('es_arrendamiento', '=', False), "
               "('partner_id', '=', partner_id)]",
        help='Cotización original en USD donde se cotizó el equipo, antes de '
             'que el cliente autorizara la renta. Obligatoria para confirmar '
             'un contrato de arrendamiento — da trazabilidad hacia el '
             'documento de origen del contrato.',
    )

    suscripciones_relacionadas_count = fields.Integer(
        string='# Suscripciones Relacionadas',
        compute='_compute_suscripciones_relacionadas_count',
        help='Número de contratos de arrendamiento que tienen esta '
             'cotización como Cotización de Origen.',
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

    def _compute_suscripciones_relacionadas_count(self):
        # Cuenta cuántas órdenes tienen a ESTA orden como cotización de
        # origen (enlace inverso: de la cotización original hacia las
        # suscripciones que se generaron a partir de ella).
        conteo = self.env['sale.order'].read_group(
            [('cotizacion_origen_id', 'in', self.ids)],
            ['cotizacion_origen_id'], ['cotizacion_origen_id'],
        )
        mapa = {c['cotizacion_origen_id'][0]: c['cotizacion_origen_id_count'] for c in conteo}
        for order in self:
            order.suscripciones_relacionadas_count = mapa.get(order.id, 0)

    def action_view_suscripciones_relacionadas(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Suscripciones Relacionadas',
            'res_model': 'sale.order',
            'view_mode': 'list,form',
            'domain': [('cotizacion_origen_id', '=', self.id)],
        }

    def action_view_cotizacion_origen(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Cotización de Origen',
            'res_model': 'sale.order',
            'view_mode': 'form',
            'res_id': self.cotizacion_origen_id.id,
        }

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
                 'tc_pactado', 'porcentaje_renta', 'currency_id', 'cuota_fijada',
                 'es_arrendamiento')
    def _compute_cuota_mensual(self):
        # La línea de servicio (ARR-GYM-MENSUAL) ya contiene el valor NETO
        # (sin IVA) del equipo, porque action_convertir_arrendamiento lo
        # calcula a partir de price_subtotal de las líneas originales, no
        # de price_unit.
        #
        # 'es_arrendamiento' se pone primero en la condición para que las
        # miles de cotizaciones normales de la empresa (que no son de
        # arrendamiento) salgan de inmediato sin iterar sus líneas. Esto
        # importa porque cuota_mensual_mxn es un campo guardado (store=True):
        # cualquier actualización del módulo que agregue una dependencia
        # nueva obliga a Odoo a recalcularlo en TODAS las órdenes de venta
        # de la base, no solo en las de arrendamiento.
        for order in self:
            if not order.es_arrendamiento or order.cuota_fijada:
                if not order.cuota_fijada:
                    order.cuota_mensual_mxn = 0.0
                continue

            total_equipo = sum(
                line.price_unit * line.product_uom_qty
                for line in order.order_line
                if line.product_id.default_code == 'ARR-GYM-MENSUAL'
            )

            if total_equipo <= 0 or order.porcentaje_renta <= 0:
                order.cuota_mensual_mxn = 0.0
                continue

            if order.currency_id.name == 'USD' and order.tc_pactado > 0:
                valor_equipo_mxn = total_equipo * order.tc_pactado
            elif order.currency_id.name == 'MXN':
                valor_equipo_mxn = total_equipo
            else:
                order.cuota_mensual_mxn = 0.0
                continue

            order.cuota_mensual_mxn = valor_equipo_mxn * (order.porcentaje_renta / 100.0)

    @api.onchange('tc_pactado', 'meses_renta', 'porcentaje_renta', 'currency_id')
    def _onchange_renta_fields(self):
        tiene_linea_renta = any(
            l.product_id.default_code == 'ARR-GYM-MENSUAL' for l in self.order_line
        )
        if not tiene_linea_renta:
            return

        if self.currency_id.name == 'USD' and not self.tc_pactado:
            return {
                'warning': {
                    'title': 'TC Pactado requerido',
                    'message': 'La cotización está en USD. '
                               'Ingresa el TC pactado para calcular la cuota mensual en MXN.',
                }
            }

        if self.currency_id.name == 'MXN' and self.tc_pactado:
            return {
                'warning': {
                    'title': '⚠ Moneda MXN con TC Pactado capturado',
                    'message': 'Esta cotización está en pesos (MXN), pero capturaste un TC '
                               'Pactado. Si el equipo se cotizó pensando en USD, la Lista de '
                               'Precios se quedó en MXN por error — cancela esta cotización y '
                               'crea una nueva seleccionando primero la Lista de Precios en USD. '
                               'Si el contrato es realmente en MXN, borra el TC Pactado.',
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

        # Base NETA (sin IVA) del equipo — el precio cotizado al cliente
        # incluye IVA, así que usamos price_subtotal, no price_unit.
        total_equipo = sum(lineas_rentables.mapped('price_subtotal'))

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

            if not order.porcentaje_renta or order.porcentaje_renta <= 0:
                raise UserError(
                    f'La cotización {order.name} no tiene un Porcentaje de Renta válido. '
                    f'Captúralo en la pestaña "Contrato de Arrendamiento" antes de confirmar.'
                )

            if not order.cotizacion_origen_id:
                raise UserError(
                    f'La cotización {order.name} no tiene una Cotización de Origen '
                    f'vinculada.\n\nCaptúrala en la pestaña "Contrato de Arrendamiento" '
                    f'antes de confirmar — es el documento en USD donde se cotizó el '
                    f'equipo antes de que el cliente autorizara la renta.'
                )

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

            # total_equipo ya es NETO (sin IVA) — viene de price_subtotal
            # de las líneas originales, capturado en la conversión.
            total_equipo = linea_servicio.price_unit

            if order.currency_id.name == 'USD':
                if not order.tc_pactado or order.tc_pactado <= 0:
                    raise UserError(
                        f'La cotización {order.name} está en USD.\n\n'
                        f'Debes ingresar el TC Pactado antes de confirmar.'
                    )
                valor_equipo_mxn = total_equipo * order.tc_pactado
                pricelist_mxn = self.env['product.pricelist'].search(
                    [('currency_id.name', '=', 'MXN')], limit=1
                )
                if pricelist_mxn:
                    order.write({'pricelist_id': pricelist_mxn.id})
            elif order.currency_id.name == 'MXN':
                if order.tc_pactado and order.tc_pactado > 0:
                    # Contradicción: si el contrato realmente es en MXN, no
                    # tiene sentido tener un TC Pactado capturado. Lo más
                    # probable es que el equipo se cotizó pensando en USD,
                    # pero la Lista de Precios de la cotización se quedó en
                    # MXN por error (no se cambió ANTES de capturar las
                    # líneas). Bloqueamos para evitar calcular la cuota sin
                    # aplicar el tipo de cambio.
                    raise UserError(
                        f'La cotización {order.name} está en MXN pero tiene un '
                        f'TC Pactado capturado ({order.tc_pactado}).\n\n'
                        f'Esto normalmente ocurre cuando el equipo se cotizó '
                        f'pensando en USD, pero la Lista de Precios de la '
                        f'cotización nunca se cambió a una en dólares antes de '
                        f'capturar las líneas.\n\n'
                        f'• Si el equipo SÍ se cotizó en USD: cancela esta '
                        f'cotización, crea una nueva, selecciona primero la '
                        f'Lista de Precios en USD y vuelve a capturar las '
                        f'líneas antes de convertir a arrendamiento.\n'
                        f'• Si el contrato realmente es en MXN: borra el TC '
                        f'Pactado (déjalo en 0) y vuelve a confirmar.'
                    )
                valor_equipo_mxn = total_equipo
            else:
                raise UserError(
                    f'La cotización {order.name} está en una moneda no '
                    f'soportada para arrendamiento ({order.currency_id.name}). '
                    f'Usa una Lista de Precios en USD o en MXN.'
                )

            cuota = valor_equipo_mxn * (order.porcentaje_renta / 100.0)
            valor_total_contrato = cuota * meses

            cuenta = order._crear_cuenta_analitica_contrato()
            linea_servicio.write({
                'price_unit': cuota,
                'analytic_distribution': {str(cuenta.id): 100},
            })

            order.write({
                'valor_equipo_mxn': valor_equipo_mxn,
                'cuota_mensual_mxn': cuota,
                'valor_contrato_mxn': valor_total_contrato,
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
        help='Precio original cotizado en USD (con IVA) antes de la conversión a arrendamiento.',
    )

    def _compute_price_unit(self):
        lineas_arrendamiento = self.filtered(lambda l: l.order_id.es_arrendamiento)
        lineas_normales = self - lineas_arrendamiento
        if lineas_normales:
            super(SaleOrderLine, lineas_normales)._compute_price_unit()

    @api.depends('qty_invoiced', 'qty_delivered', 'product_uom_qty', 'state',
                 'order_id.es_arrendamiento', 'product_id.default_code')
    def _compute_qty_to_invoice(self):
        super()._compute_qty_to_invoice()
        for line in self:
            if (line.order_id.es_arrendamiento
                    and line.product_id.default_code != 'ARR-GYM-MENSUAL'):
                line.qty_to_invoice = 0.0
