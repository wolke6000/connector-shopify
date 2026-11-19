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
