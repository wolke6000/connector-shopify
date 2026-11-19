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
        self.instance.product_export_enabled = True
        with patch.object(
            type(self.instance),
            "_shopify_client",
            side_effect=RuntimeError("product outbound boundary reached"),
        ) as allowed_client:
            with self.assertRaisesRegex(RuntimeError, "outbound boundary reached"):
                binding_model._job_export_product(
                    self.instance.id, self.product.product_tmpl_id.id
                )
        allowed_client.assert_called_once()

        self.instance.product_export_enabled = False
        with patch.object(type(self.instance), "_shopify_client") as blocked_client:
            result = binding_model._job_export_product(
                self.instance.id, self.product.product_tmpl_id.id
            )
        self.assertFalse(result)
        blocked_client.assert_not_called()

    def test_manual_product_export_is_refused(self):
        wizard = self.env["shopify.product.export.wizard"].create(
            {
                "instance_id": self.instance.id,
                "product_tmpl_ids": [(6, 0, self.product.product_tmpl_id.ids)],
            }
        )
        binding_model = self.env["shopify.product.template"]
        with patch.object(type(binding_model), "_enqueue_product_export") as enqueue:
            with self.assertRaises(UserError):
                wizard.action_export()
        enqueue.assert_not_called()

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

    def _bind_product_for_event_tests(self):
        return self.env["shopify.product.template"].create(
            {
                "instance_id": self.instance.id,
                "shopify_id": "gid://shopify/Product/9001",
                "odoo_id": self.product.product_tmpl_id.id,
                "status": "ACTIVE",
                "state": "synced",
            }
        )

    def _assert_export_trigger_blocked(self, action):
        binding_model = self.env["shopify.product.template"]
        binding_class = type(binding_model)
        original = binding_class._enqueue_product_export
        reached = []

        def trace_enqueue(recordset, instance, template):
            reached.append((instance.id, template.id))
            return original(recordset, instance, template)

        with (
            patch.object(binding_class, "_enqueue_product_export", trace_enqueue),
            patch.object(binding_class, "with_delay") as delayed,
        ):
            action()

        self.assertIn(
            (self.instance.id, self.product.product_tmpl_id.id),
            reached,
        )
        delayed.assert_not_called()

    def test_template_change_reaches_policy_without_queuing(self):
        self._bind_product_for_event_tests()
        self._assert_export_trigger_blocked(
            lambda: self.product.product_tmpl_id.write({"name": "Changed name"})
        )

    def test_variant_change_reaches_policy_without_queuing(self):
        binding = self._bind_product_for_event_tests()
        self.env["shopify.product.variant"].create(
            {
                "instance_id": self.instance.id,
                "shopify_id": "gid://shopify/ProductVariant/9002",
                "template_binding_id": binding.id,
                "odoo_id": self.product.id,
                "state": "synced",
            }
        )
        self._assert_export_trigger_blocked(
            lambda: self.product.write({"default_code": "NO-EXPORT"})
        )

    def test_pricelist_change_reaches_policy_without_queuing(self):
        self._bind_product_for_event_tests()
        self._assert_export_trigger_blocked(
            lambda: self.env["product.pricelist.item"].create(
                {
                    "pricelist_id": self.instance.product_pricelist_id.id,
                    "compute_price": "fixed",
                    "fixed_price": 19,
                    "applied_on": "1_product",
                    "product_tmpl_id": self.product.product_tmpl_id.id,
                }
            )
        )

    def test_product_image_change_reaches_policy_without_queuing(self):
        self._bind_product_for_event_tests()
        self._assert_export_trigger_blocked(
            lambda: self.env["product.image"].create(
                {
                    "name": "No Shopify upload",
                    "product_tmpl_id": self.product.product_tmpl_id.id,
                }
            )
        )

