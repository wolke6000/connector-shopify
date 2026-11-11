from unittest.mock import Mock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from ..wizards.onboarding import (
    INVENTORY_ONLY_ADMIN_SCOPES,
    REQUIRED_ADMIN_SCOPES,
)


class TestShopifyWritePolicy(TransactionCase):
    def test_inventory_only_scope_requirements(self):
        wizard = self.env["shopify.instance.wizard"]
        required = set(wizard._required_admin_scopes_for_policy("inventory_only"))
        self.assertEqual(required, set(INVENTORY_ONLY_ADMIN_SCOPES))
        self.assertTrue(
            {"write_inventory", "read_fulfillments", "read_returns"} <= required
        )
        self.assertFalse(
            {"write_products", "write_customers",
             "write_merchant_managed_fulfillment_orders"} & required
        )

    def test_full_scope_requirements_are_preserved(self):
        wizard = self.env["shopify.instance.wizard"]
        self.assertEqual(
            tuple(wizard._required_admin_scopes_for_policy("full")),
            REQUIRED_ADMIN_SCOPES,
        )

    def test_inventory_only_blocks_customer_export(self):
        instance = self.env["shopify.instance"].create({
            "name": "Read-only customers",
            "shop_url": "inventory-only-test.myshopify.com",
            "access_token": "test-token",
            "webhook_secret": "test-secret",
            "shopify_write_policy": "inventory_only",
            "customer_export_enabled": True,
        })
        binding = self.env["shopify.customer"].create({
            "instance_id": instance.id,
            "shopify_id": "gid://shopify/Customer/12345",
            "odoo_id": self.env.company.partner_id.id,
        })
        client = Mock()
        with patch.object(type(instance), "_shopify_client", return_value=client):
            self.assertFalse(binding._job_export_customer())
        client.execute.assert_not_called()

    def test_inventory_only_blocks_manual_product_exports(self):
        instance = self.env["shopify.instance"].create({
            "name": "Inventory only product",
            "shop_url": "inventory-only-export.myshopify.com",
            "access_token": "test-token",
            "webhook_secret": "test-secret",
            "shopify_write_policy": "inventory_only",
        })
        wizard = self.env["shopify.product.export.wizard"].create({
            "instance_id": instance.id,
            "product_tmpl_ids": [(6, 0, [self.env.ref("product.product_product_4").product_tmpl_id.id])],
        })
        with self.assertRaises(UserError):
            wizard.action_export()
