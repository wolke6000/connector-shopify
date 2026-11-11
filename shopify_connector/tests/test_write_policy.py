from unittest.mock import Mock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from ..wizards.onboarding import (
    INVENTORY_ONLY_ADMIN_SCOPES,
    REQUIRED_ADMIN_SCOPES,
)


class TestShopifyWritePolicy(TransactionCase):
    def _instance(self, name, **values):
        return self.env["shopify.instance"].create(
            {
                "name": name,
                "shop_url": f"{name}.myshopify.com",
                "access_token": "test-token",
                "webhook_secret": "test-secret",
                "shopify_write_policy": "inventory_only",
                **values,
            }
        )

    def test_inventory_only_scopes(self):
        wizard = self.env["shopify.instance.wizard"]
        required = set(wizard._required_admin_scopes_for_policy("inventory_only"))
        self.assertEqual(required, set(INVENTORY_ONLY_ADMIN_SCOPES))
        self.assertTrue(
            {"write_inventory", "read_fulfillments", "read_returns"} <= required
        )
        self.assertFalse(
            {
                "write_products",
                "write_customers",
                "write_merchant_managed_fulfillment_orders",
            }
            & required
        )

    def test_full_scopes_remain_available(self):
        wizard = self.env["shopify.instance.wizard"]
        self.assertEqual(
            tuple(wizard._required_admin_scopes_for_policy("full")),
            REQUIRED_ADMIN_SCOPES,
        )

    def test_customer_export_job_is_blocked(self):
        instance = self._instance("customer-write-policy", customer_export_enabled=True)
        binding = self.env["shopify.customer"].create(
            {
                "instance_id": instance.id,
                "shopify_id": "gid://shopify/Customer/12345",
                "odoo_id": self.env.company.partner_id.id,
            }
        )
        client = Mock()
        with patch.object(type(instance), "_shopify_client", return_value=client):
            self.assertFalse(binding._job_export_customer())
        client.execute.assert_not_called()

    def test_product_export_job_is_blocked(self):
        instance = self._instance("product-write-policy")
        template = self.env["product.template"].create(
            {"name": "Test product", "company_id": self.env.company.id}
        )
        client = Mock()
        with patch.object(type(instance), "_shopify_client", return_value=client):
            self.assertFalse(
                self.env["shopify.product.template"]._job_export_product(
                    instance.id, template.id
                )
            )
        client.execute.assert_not_called()

    def test_manual_product_export_is_blocked(self):
        instance = self._instance("manual-write-policy")
        template = self.env["product.template"].create(
            {"name": "Manual product", "company_id": self.env.company.id}
        )
        wizard = self.env["shopify.product.export.wizard"].create(
            {"instance_id": instance.id, "product_tmpl_ids": [(6, 0, [template.id])]}
        )
        with self.assertRaises(UserError):
            wizard.action_export()
