"""Regression checks for Switchology outbound Shopify export policy."""

from unittest.mock import Mock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestShopifyExportPolicy(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env["shopify.instance"].create(
            {
                "name": "Read-only shop",
                "shop_url": "read-only-shop.myshopify.com",
                "access_token": "test-token",
                "webhook_secret": "test-secret",
            }
        )
        cls.instance.state = "connected"
        cls.product = cls.env["product.product"].create(
            {
                "name": "Local product",
                "company_id": cls.instance.company_id.id,
            }
        )

    def test_new_instances_disable_both_writes(self):
        self.assertFalse(self.instance.product_export_enabled)
        self.assertFalse(self.instance.fulfillment_export_enabled)

    def test_disabled_product_export_does_not_enqueue(self):
        binding_model = self.env["shopify.product.template"]
        with patch.object(type(binding_model), "with_delay") as delayed:
            result = binding_model._enqueue_product_export(
                self.instance, self.product.product_tmpl_id
            )
        self.assertFalse(result)
        delayed.assert_not_called()

    def test_existing_queued_product_export_does_not_use_client(self):
        binding_model = self.env["shopify.product.template"]
        with patch.object(type(self.instance), "_shopify_client") as client:
            result = binding_model._job_export_product(
                self.instance.id, self.product.product_tmpl_id.id
            )
        self.assertFalse(result)
        client.assert_not_called()

    def test_manual_product_export_is_refused(self):
        wizard = self.env["shopify.product.export.wizard"].create(
            {
                "instance_id": self.instance.id,
                "product_tmpl_ids": [(6, 0, self.product.product_tmpl_id.ids)],
            }
        )
        with self.assertRaises(UserError):
            wizard.action_export()

    def test_explicit_opt_in_preserves_normal_product_queue(self):
        self.instance.product_export_enabled = True
        binding_model = self.env["shopify.product.template"]
        delayed = Mock()
        with patch.object(
            type(binding_model), "with_delay", return_value=delayed
        ) as with_delay:
            binding_model._enqueue_product_export(
                self.instance, self.product.product_tmpl_id
            )
        with_delay.assert_called_once()
        delayed._job_export_product.assert_called_once_with(
            self.instance.id, self.product.product_tmpl_id.id
        )
