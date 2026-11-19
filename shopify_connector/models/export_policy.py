"""Switchology outbound write gates kept separate from OCA synchronization logic.

The checks in queued jobs are deliberate: disabling a switch must also stop
jobs that were queued while the switch was enabled.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError


class ShopifyInstanceExportPolicy(models.Model):
    _inherit = "shopify.instance"

    product_export_enabled = fields.Boolean(
        string="Export Products to Shopify",
        default=False,
        help="Permit automatic and manual product exports. Inventory is independent.",
    )


class ShopifyProductExportPolicy(models.Model):
    _inherit = "shopify.product.template"

    @api.model
    def _enqueue_product_export(self, instance, template):
        if not instance.product_export_enabled:
            return False
        return super()._enqueue_product_export(instance, template)

    @api.model
    def _job_export_product(self, instance_id, template_id):
        instance = self.env["shopify.instance"].sudo().browse(instance_id).exists()
        if not instance or not instance.product_export_enabled:
            return False
        return super()._job_export_product(instance_id, template_id)


class ShopifyOrderFulfillmentExportPolicy(models.Model):
    _inherit = "shopify.order"

    def _job_push_picking_fulfillment(self, picking_id):
        self.ensure_one()
        if not self.instance_id.fulfillment_export_enabled:
            return False
        return super()._job_push_picking_fulfillment(picking_id)

    def _push_picking_fulfillment(self, picking_id):
        """Also guard the direct helper introduced by the OCA upstream."""
        self.ensure_one()
        if not self.instance_id.fulfillment_export_enabled:
            return False
        return super()._push_picking_fulfillment(picking_id)

    def _queue_missed_fulfillment_pushes(self):
        """Applies after the OCA upstream integration adds the retry queue."""
        self.ensure_one()
        if not self.instance_id.fulfillment_export_enabled:
            return False
        return super()._queue_missed_fulfillment_pushes()


class ShopifyFulfillmentOrderExportPolicy(models.Model):
    _inherit = "shopify.fulfillment.order"

    def action_release_hold(self):
        if any(not item.instance_id.fulfillment_export_enabled for item in self):
            raise UserError(self.env._("Shopify fulfillment export is disabled."))
        return super().action_release_hold()

    def _job_release_hold(self):
        self.ensure_one()
        if not self.instance_id.fulfillment_export_enabled:
            return False
        return super()._job_release_hold()


class ShopifyFulfillmentExportPolicy(models.Model):
    _inherit = "shopify.fulfillment"

    def action_cancel_shopify(self):
        if any(not item.instance_id.fulfillment_export_enabled for item in self):
            raise UserError(self.env._("Shopify fulfillment export is disabled."))
        return super().action_cancel_shopify()

    def _job_cancel_shopify(self):
        self.ensure_one()
        if not self.instance_id.fulfillment_export_enabled:
            return False
        return super()._job_cancel_shopify()

    def _job_update_tracking(self):
        """Applies after the OCA upstream integration adds tracking updates."""
        self.ensure_one()
        if not self.instance_id.fulfillment_export_enabled:
            return False
        return super()._job_update_tracking()


class StockPickingFulfillmentExportPolicy(models.Model):
    _inherit = "stock.picking"

    def _queue_shopify_tracking_update(self):
        """Applies after the OCA upstream integration adds tracking updates."""
        # Avoid writing back tracking for Shopify-authoritative fulfillments.
        if not self.sudo().shopify_fulfillment_ids.filtered(
            lambda item: item.instance_id.fulfillment_export_enabled
        ):
            return False
        return super()._queue_shopify_tracking_update()
