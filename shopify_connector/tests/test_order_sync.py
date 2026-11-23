from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

from freezegun import freeze_time

from odoo import SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.sql_db import db_connect

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.tests.common import trap_jobs
from odoo.addons.shopify_connector.graphql.order import (
    ADDRESS_FIELDS,
    DRAFT_ORDER_BY_ID_QUERY,
    DRAFT_ORDERS_QUERY,
    ORDER_BY_ID_QUERY,
    orders_updated_search,
)
from odoo.addons.shopify_connector.lib.client import ShopifyError
from odoo.addons.shopify_connector.lib.order import normalize_order_payload


class TestShopifyOrderSync(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = (
            cls.env["base"]
            .with_user(SUPERUSER_ID)
            .with_company(cls.company_data["company"])
            .env
        )
        cls.instance = cls.env["shopify.instance"].create(
            {
                "name": "Order Shop",
                "shop_url": "order-shop.myshopify.com",
                "access_token": "test-token",
                "webhook_secret": "test-secret",
                "unknown_product_policy": "block",
                "order_import_date_from": "2026-01-01",
            }
        )
        cls.instance.state = "connected"
        cls.currency = cls.instance.company_id.currency_id
        cls.bank_journal = cls.company_data["default_journal_bank"]
        cls.sale_journal = cls.company_data["default_journal_sale"]
        cls.product = cls.env["product.product"].create(
            {
                "name": "Bound Order Product",
                "type": "service",
                "company_id": cls.instance.company_id.id,
            }
        )
        cls.template_binding = cls.env["shopify.product.template"].create(
            {
                "instance_id": cls.instance.id,
                "shopify_id": "gid://shopify/Product/10",
                "odoo_id": cls.product.product_tmpl_id.id,
                "status": "ACTIVE",
            }
        )
        cls.variant_binding = cls.env["shopify.product.variant"].create(
            {
                "instance_id": cls.instance.id,
                "shopify_id": "gid://shopify/ProductVariant/20",
                "template_binding_id": cls.template_binding.id,
                "odoo_id": cls.product.id,
            }
        )
        cls.order_model = cls.env["shopify.order"]
        cls.goods = cls.env["product.product"].create(
            {
                "name": "Bound Goods Product",
                "type": "consu",
                "is_storable": True,
                "company_id": cls.instance.company_id.id,
            }
        )
        cls.goods_variant_binding = cls.env["shopify.product.variant"].create(
            {
                "instance_id": cls.instance.id,
                "shopify_id": "gid://shopify/ProductVariant/21",
                "template_binding_id": cls.env["shopify.product.template"]
                .create(
                    {
                        "instance_id": cls.instance.id,
                        "shopify_id": "gid://shopify/Product/11",
                        "odoo_id": cls.goods.product_tmpl_id.id,
                        "status": "ACTIVE",
                    }
                )
                .id,
                "odoo_id": cls.goods.id,
            }
        )

    def _bag(self, amount):
        return {
            "shopMoney": {
                "amount": amount,
                "currencyCode": self.currency.name,
            },
            "presentmentMoney": {
                "amount": amount,
                "currencyCode": self.currency.name,
            },
        }

    def _payload(self, order_id=1, quantity=1, total="10.00", **values):
        payload = {
            "id": f"gid://shopify/Order/{order_id}",
            "name": f"#{order_id}",
            "createdAt": "2026-01-10T10:00:00Z",
            "updatedAt": "2026-01-10T10:00:00Z",
            "currencyCode": self.currency.name,
            "presentmentCurrencyCode": self.currency.name,
            "displayFinancialStatus": "PENDING",
            "displayFulfillmentStatus": "UNFULFILLED",
            "currentSubtotalPriceSet": self._bag(total),
            "currentTotalDiscountsSet": self._bag("0.00"),
            "currentShippingPriceSet": self._bag("0.00"),
            "currentTotalTaxSet": self._bag("0.00"),
            "currentTotalPriceSet": self._bag(total),
            "lineItems": {
                "nodes": [
                    {
                        "id": "gid://shopify/LineItem/30",
                        "name": "Bound Order Product",
                        "sku": "ORDER-1",
                        "quantity": quantity,
                        "currentQuantity": quantity,
                        "refundableQuantity": quantity,
                        "variant": {"id": self.variant_binding.shopify_id},
                        "product": {"id": self.template_binding.shopify_id},
                        "originalUnitPriceSet": self._bag("10.00"),
                        "discountedUnitPriceAfterAllDiscountsSet": self._bag("10.00"),
                        "taxLines": [],
                        "discountAllocations": [],
                    }
                ]
            },
            "shippingLines": {"nodes": []},
            "transactions": [],
            "refunds": {"nodes": []},
            "tags": ["web"],
            "discountCodes": [],
        }
        payload.update(values)
        return payload

    def test_legacy_variant_without_id_resolves_unique_sku(self):
        # SW-OCA-009: preserve historical products when Shopify no longer
        # exposes the original variant ID.
        self.product.default_code = "SW-LEGACY-UNIQUE-94"
        line = {
            "gift_card": False,
            "variant_id": False,
            "product_id": False,
            "sku": "SW-LEGACY-UNIQUE-94",
        }
        self.assertEqual(
            self.order_model._line_product(self.instance, line), self.product
        )

    def test_legacy_variant_without_id_rejects_ambiguous_sku(self):
        # Do not silently assign an order line to an arbitrary Odoo product.
        sku = "SW-LEGACY-DUPLICATE-95"
        self.env["product.product"].create(
            {
                "name": "Legacy SKU A",
                "type": "service",
                "company_id": self.instance.company_id.id,
                "default_code": sku,
            }
        )
        self.env["product.product"].create(
            {
                "name": "Legacy SKU B",
                "type": "service",
                "company_id": self.instance.company_id.id,
                "default_code": sku,
            }
        )
        line = {
            "gift_card": False,
            "variant_id": False,
            "product_id": False,
            "sku": sku,
        }
        with self.assertRaises(UserError):
            self.order_model._line_product(self.instance, line)

    def test_legacy_custom_line_uses_named_fallback_product(self):
        # Only lines without variant, product and SKU can use this fallback.
        custom = self.env["product.product"].create(
            {
                "name": "Legacy Shopify Custom Line",
                "type": "service",
                "company_id": self.instance.company_id.id,
                "default_code": "SHOPIFY-LEGACY-CUSTOM",
            }
        )
        line = {
            "gift_card": False,
            "variant_id": False,
            "product_id": False,
            "sku": "",
        }
        self.assertEqual(self.order_model._line_product(self.instance, line), custom)

    def _cancelled_payload(self, order_id, **values):
        """Return the Shopify payload after cancellation.

        Shopify retains ordered quantities but zeroes the current
        quantity and totals of each line.
        """
        payload = self._payload(
            order_id=order_id,
            total="0.00",
            cancelledAt="2026-01-12T10:00:00Z",
            updatedAt="2026-01-12T10:00:00Z",
            **values,
        )
        for line in payload["lineItems"]["nodes"]:
            line["currentQuantity"] = 0
            line["refundableQuantity"] = 0
        return payload

    def test_cutover_blocks_unbound_old_order_even_when_updated_recently(self):
        self.instance.order_import_date_from = "2026-02-01"
        before = self.order_model.search_count([])
        self.assertFalse(
            self.order_model._job_import_order(
                self.instance.id,
                self._payload(order_id=901, updatedAt="2026-11-10T10:00:00Z"),
            )
        )
        self.assertEqual(self.order_model.search_count([]), before)

    def test_cutover_blocks_reimport_of_deleted_historical_binding(self):
        # SW-OCA-001: simulate the verified one-off cleanup and a late webhook.
        old_payload = self._payload(order_id=907)
        binding_id = self.order_model._job_import_order(self.instance.id, old_payload)
        binding = self.order_model.browse(binding_id)
        sale_order = binding.odoo_id
        self.assertTrue(sale_order)
        sale_order_id = sale_order.id

        # The production cleanup removed both technical bindings and sale orders.
        binding.unlink()
        sale_order.unlink()
        self.assertFalse(self.order_model.browse(binding_id).exists())
        self.assertFalse(self.env["sale.order"].browse(sale_order_id).exists())

        self.instance.order_import_date_from = "2026-11-07"
        updated_payload = self._payload(order_id=907, updatedAt="2026-11-19T18:26:00Z")
        client = Mock()
        client.execute.return_value = {"order": updated_payload}
        with patch.object(type(self.instance), "_shopify_client", return_value=client):
            self.assertFalse(
                self.order_model._job_import_order_from_api(self.instance.id, 907)
            )

        client.execute.assert_called_once_with(
            ORDER_BY_ID_QUERY, {"id": "gid://shopify/Order/907"}
        )
        self.assertFalse(
            self.order_model.search(
                [
                    ("instance_id", "=", self.instance.id),
                    ("shopify_id", "=", "gid://shopify/Order/907"),
                ]
            )
        )
        self.assertFalse(self.env["sale.order"].browse(sale_order_id).exists())

    def test_cutover_allows_updates_of_synced_old_orders(self):
        binding_id = self.order_model._job_import_order(
            self.instance.id, self._payload(order_id=902)
        )
        self.instance.order_import_date_from = "2026-02-01"
        self.assertEqual(
            self.order_model._job_import_order(
                self.instance.id,
                self._payload(
                    order_id=902,
                    quantity=2,
                    total="20.00",
                    updatedAt="2026-11-10T10:00:00Z",
                ),
            ),
            binding_id,
        )
        self.assertEqual(
            self.order_model.browse(binding_id).odoo_id.order_line.product_uom_qty,
            2,
        )

    def test_cutover_retries_previously_synced_old_order_after_error(self):
        # SW-OCA-001: transient failure must not revoke a successful binding.
        original = self._payload(order_id=908)
        binding_id = self.order_model._job_import_order(self.instance.id, original)
        binding = self.order_model.browse(binding_id)
        self.assertEqual(binding.state, "synced")
        self.assertTrue(binding.odoo_id)
        self.assertTrue(binding.payload_digest)

        self.instance.order_import_date_from = "2026-02-01"
        changed = self._payload(
            order_id=908,
            quantity=2,
            total="20.00",
            updatedAt="2026-11-10T10:00:00Z",
        )
        with patch.object(
            type(self.order_model), "_build_order", side_effect=ValueError("temporary")
        ):
            self.assertFalse(
                self.order_model._job_import_order(self.instance.id, changed)
            )
        self.assertEqual(binding.state, "error")
        self.assertTrue(binding.odoo_id)
        self.assertTrue(binding.payload_digest)

        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, changed), binding_id
        )
        self.assertEqual(binding.state, "synced")
        self.assertEqual(binding.odoo_id.order_line.product_uom_qty, 2)

    def test_cutover_drift_requeues_previously_synced_old_order_in_error(self):
        original = self._payload(order_id=909)
        binding_id = self.order_model._job_import_order(self.instance.id, original)
        binding = self.order_model.browse(binding_id)
        binding.state = "error"
        self.instance.order_import_date_from = "2026-02-01"
        self.instance.order_drift_since = datetime(2026, 11, 8)
        gid = "gid://shopify/Order/909"
        client = Mock()
        client.execute.return_value = {
            "orders": {
                "nodes": [
                    {
                        "id": gid,
                        "name": "#909",
                        "createdAt": "2026-01-10T10:00:00Z",
                        "updatedAt": "2026-11-10T10:00:00Z",
                    }
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
        with (
            trap_jobs() as trap,
            patch.object(type(self.instance), "_shopify_client", return_value=client),
        ):
            self.assertEqual(self.instance._job_sync_updated_orders(), 1)
        trap.assert_jobs_count(1, only=self.order_model._job_import_order_from_api)
        self.assertIn(gid, self.instance.order_drift_pending)

    def test_cutover_does_not_retry_failed_old_order(self):
        self.assertFalse(
            self.order_model._job_import_order(
                self.instance.id, self._payload(order_id=903, total="10.02")
            )
        )
        binding = self.order_model.search(
            [
                ("instance_id", "=", self.instance.id),
                ("shopify_id", "=", "gid://shopify/Order/903"),
            ]
        )
        self.assertEqual(binding.state, "error")
        self.instance.order_import_date_from = "2026-02-01"
        self.assertFalse(
            self.order_model._job_import_order(
                self.instance.id, self._payload(order_id=903)
            )
        )
        self.assertEqual(binding.state, "error")
        self.assertFalse(binding.odoo_id)

    def test_cutover_drift_skips_old_unknown_orders(self):
        self.instance.order_import_date_from = "2026-11-08"
        self.instance.order_drift_since = datetime(2026, 11, 8)
        old_gid = "gid://shopify/Order/904"
        new_gid = "gid://shopify/Order/905"
        client = Mock()
        client.execute.return_value = {
            "orders": {
                "nodes": [
                    {
                        "id": old_gid,
                        "name": "#904",
                        "createdAt": "2026-11-07T23:59:59Z",
                        "updatedAt": "2026-11-10T10:00:00Z",
                    },
                    {
                        "id": new_gid,
                        "name": "#905",
                        "createdAt": "2026-11-08T00:00:00Z",
                        "updatedAt": "2026-11-10T10:00:00Z",
                    },
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
        with (
            trap_jobs() as trap,
            patch.object(type(self.instance), "_shopify_client", return_value=client),
        ):
            self.assertEqual(self.instance._job_sync_updated_orders(), 1)
        trap.assert_jobs_count(1, only=self.order_model._job_import_order_from_api)
        self.assertNotIn(old_gid, self.instance.order_drift_pending)
        self.assertIn(new_gid, self.instance.order_drift_pending)

    def test_cutover_without_date_blocks_unbound_orders(self):
        self.instance.order_import_date_from = False
        self.assertFalse(
            self.order_model._job_import_order(
                self.instance.id, self._payload(order_id=906)
            )
        )
        self.assertFalse(
            self.order_model.search(
                [
                    ("instance_id", "=", self.instance.id),
                    ("shopify_id", "=", "gid://shopify/Order/906"),
                ]
            )
        )

    def test_order_build_and_edit_are_idempotent(self):
        binding_id = self.order_model._job_import_order(
            self.instance.id, self._payload()
        )
        binding = self.order_model.browse(binding_id)

        self.assertEqual(binding.odoo_id.amount_total, 10)
        self.assertEqual(len(binding.line_binding_ids), 1)

        self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                quantity=2,
                total="20.00",
                updatedAt="2026-01-11T10:00:00Z",
            ),
        )
        self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                quantity=2,
                total="20.00",
                updatedAt="2026-01-11T10:00:00Z",
            ),
        )

        self.assertEqual(binding.odoo_id.order_line.product_uom_qty, 2)
        self.assertEqual(len(binding.line_binding_ids), 1)

    def test_order_imports_its_customer(self):
        payload = self._payload(order_id=14)
        payload["customer"] = {"id": "gid://shopify/Customer/140"}
        client = Mock()
        client.execute.return_value = {
            "customer": {
                "id": "gid://shopify/Customer/140",
                "firstName": "Order",
                "lastName": "Buyer",
                "defaultEmailAddress": {"emailAddress": "order.buyer@example.com"},
            }
        }

        with patch.object(type(self.instance), "_shopify_client", return_value=client):
            binding_id = self.order_model._job_import_order(self.instance.id, payload)

        partner = self.order_model.browse(binding_id).odoo_id.partner_id
        self.assertNotEqual(partner, self.instance.guest_partner_id)
        self.assertEqual(partner.email, "order.buyer@example.com")
        self.assertEqual(
            partner.shopify_customer_binding_ids.shopify_id,
            "gid://shopify/Customer/140",
        )

    def _address(self, street, city="Veendam", zip_code="9641 AA", **values):
        return {
            "firstName": "Anna",
            "lastName": "de Vries",
            "company": None,
            "address1": street,
            "address2": None,
            "city": city,
            "provinceCode": None,
            "countryCodeV2": "NL",
            "zip": zip_code,
            "phone": "+31612345678",
            **values,
        }

    def test_order_imports_its_delivery_and_invoice_address(self):
        payload = self._payload(
            order_id=15,
            shippingAddress=self._address("Leveringsweg 2"),
            billingAddress=self._address("Factuurstraat 1", city="Groningen"),
        )

        binding_id = self.order_model._job_import_order(self.instance.id, payload)

        order = self.order_model.browse(binding_id).odoo_id
        shipping = order.partner_shipping_id
        invoice = order.partner_invoice_id
        self.assertEqual(shipping.parent_id, order.partner_id)
        self.assertEqual(shipping.type, "delivery")
        self.assertEqual(shipping.name, "Anna de Vries")
        self.assertEqual(shipping.street, "Leveringsweg 2")
        self.assertEqual(shipping.zip, "9641 AA")
        self.assertEqual(shipping.city, "Veendam")
        self.assertEqual(shipping.country_id.code, "NL")
        self.assertEqual(shipping.phone, "+31612345678")
        self.assertEqual(invoice.type, "invoice")
        self.assertEqual(invoice.street, "Factuurstraat 1")
        self.assertEqual(invoice.city, "Groningen")

    def test_reimported_order_reuses_its_address_contacts(self):
        values = {
            "shippingAddress": self._address("Leveringsweg 2"),
            "billingAddress": self._address("Leveringsweg 2"),
        }
        binding_id = self.order_model._job_import_order(
            self.instance.id, self._payload(order_id=16, **values)
        )
        order = self.order_model.browse(binding_id).odoo_id
        contacts = order.partner_id.child_ids

        self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=16,
                quantity=2,
                total="20.00",
                updatedAt="2026-01-11T10:00:00Z",
                **values,
            ),
        )

        self.assertEqual(order.partner_id.child_ids, contacts)
        self.assertEqual(len(contacts), 1)
        self.assertEqual(order.partner_shipping_id, contacts)
        self.assertEqual(order.partner_invoice_id, contacts)

    def test_shipping_address_edited_in_shopify_reaches_the_order(self):
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(order_id=17, shippingAddress=self._address("Oud 1")),
        )
        order = self.order_model.browse(binding_id).odoo_id

        self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=17,
                updatedAt="2026-01-11T10:00:00Z",
                shippingAddress=self._address("Nieuw 9"),
            ),
        )

        self.assertEqual(order.partner_shipping_id.street, "Nieuw 9")

    def test_order_without_address_ships_to_the_customer(self):
        binding_id = self.order_model._job_import_order(
            self.instance.id, self._payload(order_id=18)
        )

        order = self.order_model.browse(binding_id).odoo_id
        self.assertEqual(order.partner_shipping_id, order.partner_id)
        self.assertEqual(order.partner_invoice_id, order.partner_id)

    def test_guest_order_reuses_the_matching_contact_among_many(self):
        guest = self.instance.guest_partner_id
        self.assertTrue(guest)
        netherlands = self.env.ref("base.nl")
        self.env["res.partner"].create(
            [
                {
                    "parent_id": guest.id,
                    "type": "delivery",
                    "name": "Anna de Vries",
                    "street": f"Leveringsweg {number}",
                    "city": "Veendam",
                    "zip": "9641 AA",
                    "country_id": netherlands.id,
                }
                for number in range(10, 40)
            ]
        )
        same = self.env["res.partner"].create(
            {
                "parent_id": guest.id,
                "type": "delivery",
                "name": "anna de vries",
                "street": " LEVERINGSWEG 2 ",
                "phone": "+31612345678",
                "city": "Veendam",
                "zip": "9641 AA",
                "country_id": netherlands.id,
            }
        )

        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(order_id=20, shippingAddress=self._address("Leveringsweg 2")),
        )

        order = self.order_model.browse(binding_id).odoo_id
        self.assertEqual(order.partner_id, guest)
        self.assertEqual(order.partner_shipping_id, same)

    def test_total_verification_failure_leaves_no_sale_order(self):
        before = self.env["sale.order"].search_count([])

        result = self.order_model._job_import_order(
            self.instance.id, self._payload(order_id=2, total="10.02")
        )

        binding = self.order_model.search(
            [
                ("instance_id", "=", self.instance.id),
                ("shopify_id", "=", "gid://shopify/Order/2"),
            ]
        )
        self.assertFalse(result)
        self.assertEqual(binding.state, "error")
        self.assertFalse(binding.odoo_id)
        self.assertEqual(self.env["sale.order"].search_count([]), before)

    def test_subtotal_rounding_adjustment_accepts_one_currency_unit(self):
        result = self.order_model._job_import_order(
            self.instance.id, self._payload(order_id=9, total="10.01")
        )

        binding = self.order_model.browse(result)
        order = binding.odoo_id
        adjustment = order.order_line.filtered(
            lambda line: line.shopify_line_id
            == "shopify-subtotal-adjustment:gid://shopify/Order/9"
        )

        self.assertTrue(result)
        self.assertEqual(binding.state, "synced")
        self.assertEqual(len(adjustment), 1)
        self.assertAlmostEqual(order.amount_total, 10.01, places=2)
        self.assertAlmostEqual(adjustment.price_unit, 0.01, places=2)
        self.assertFalse(adjustment.tax_ids)

    def test_no_discount_multi_unit_price_mismatch_is_not_rounding(self):
        # SW-OCA-009: never hide an actual two-cent price difference merely
        # because an order contains several units and no discount.
        payload = self._payload(order_id=910, quantity=2, total="20.02")
        for field in ("currentSubtotalPriceSet", "currentTotalPriceSet"):
            payload[field] = self._bag("20.02")

        before = self.env["sale.order"].search_count([])
        result = self.order_model._job_import_order(self.instance.id, payload)

        binding = self.order_model.search(
            [
                ("instance_id", "=", self.instance.id),
                ("shopify_id", "=", "gid://shopify/Order/910"),
            ]
        )
        self.assertFalse(result)
        self.assertEqual(binding.state, "error")
        self.assertIn("Order total mismatch", binding.error_message)
        self.assertFalse(binding.odoo_id)
        self.assertEqual(self.env["sale.order"].search_count([]), before)

    def test_confirmed_order_removes_and_restores_duty_line(self):
        self.instance.order_confirmation_policy = "auto"
        first = self._payload(
            order_id=611,
            total="12.00",
            currentTotalDutiesSet=self._bag("2.00"),
            displayFinancialStatus="PAID",
        )
        binding = self.order_model.browse(
            self.order_model._job_import_order(self.instance.id, first)
        )
        sale = binding.odoo_id
        duty = sale.order_line.filtered("shopify_is_duty")
        self.assertEqual(sale.state, "sale")
        self.assertEqual(len(duty), 1)
        self.assertAlmostEqual(sale.amount_total, 12.00)

        removed = self._payload(
            order_id=611,
            total="10.00",
            currentTotalDutiesSet=self._bag("0.00"),
            displayFinancialStatus="PAID",
            updatedAt="2026-01-11T10:00:00Z",
        )
        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, removed),
            binding.id,
            binding.error_message,
        )
        self.assertEqual(binding.state, "synced")
        self.assertEqual(sale.order_line.filtered("shopify_is_duty"), duty)
        self.assertEqual(duty.product_uom_qty, 0)
        self.assertEqual(duty.shopify_expected_total, "0")
        self.assertAlmostEqual(sale.amount_total, 10.00)
        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, removed), binding.id
        )
        self.assertEqual(len(sale.order_line.filtered("shopify_is_duty")), 1)

        restored = self._payload(
            order_id=611,
            total="12.00",
            currentTotalDutiesSet=self._bag("2.00"),
            displayFinancialStatus="PAID",
            updatedAt="2026-01-12T10:00:00Z",
        )
        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, restored),
            binding.id,
            binding.error_message,
        )
        self.assertEqual(sale.order_line.filtered("shopify_is_duty"), duty)
        self.assertEqual(duty.product_uom_qty, 1)
        self.assertAlmostEqual(sale.amount_total, 12.00)

    def test_confirmed_order_removes_and_restores_shipping_line(self):
        self.instance.order_confirmation_policy = "auto"
        shipping = {
            "id": "gid://shopify/ShippingLine/612",
            "title": "Shipping",
            "originalPriceSet": self._bag("2.00"),
            "currentDiscountedPriceSet": self._bag("2.00"),
            "taxLines": [],
        }
        initial = self._payload(
            order_id=612,
            total="12.00",
            currentShippingPriceSet=self._bag("2.00"),
            shippingLines={"nodes": [shipping]},
            displayFinancialStatus="PAID",
        )
        binding = self.order_model.browse(
            self.order_model._job_import_order(self.instance.id, initial)
        )
        sale = binding.odoo_id
        shipping_line = binding.line_binding_ids.filtered("is_shipping").odoo_id
        self.assertEqual(sale.state, "sale")
        self.assertEqual(len(shipping_line), 1)

        removed = self._payload(
            order_id=612,
            total="10.00",
            updatedAt="2026-01-11T10:00:00Z",
            displayFinancialStatus="PAID",
        )
        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, removed),
            binding.id,
            binding.error_message,
        )
        self.assertEqual(shipping_line.product_uom_qty, 0)
        self.assertEqual(len(binding.line_binding_ids.filtered("is_shipping")), 1)
        self.assertAlmostEqual(sale.amount_total, 10.00)

        restored = self._payload(
            order_id=612,
            total="12.00",
            currentShippingPriceSet=self._bag("2.00"),
            shippingLines={"nodes": [shipping]},
            updatedAt="2026-01-12T10:00:00Z",
            displayFinancialStatus="PAID",
        )
        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, restored),
            binding.id,
            binding.error_message,
        )
        self.assertEqual(shipping_line.product_uom_qty, 1)
        self.assertEqual(len(binding.line_binding_ids.filtered("is_shipping")), 1)
        self.assertAlmostEqual(sale.amount_total, 12.00)

    def test_confirmed_order_removes_and_restores_rounding_line(self):
        self.instance.order_confirmation_policy = "auto"
        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id,
                self._payload(
                    order_id=613, total="10.01", displayFinancialStatus="PAID"
                ),
            )
        )
        sale = binding.odoo_id
        adjustment = sale.order_line.filtered(
            lambda line: line.shopify_line_id
            == "shopify-subtotal-adjustment:gid://shopify/Order/613"
        )
        self.assertEqual(sale.state, "sale")
        self.assertEqual(len(adjustment), 1)

        removed = self._payload(
            order_id=613,
            total="10.00",
            updatedAt="2026-01-11T10:00:00Z",
            displayFinancialStatus="PAID",
        )
        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, removed),
            binding.id,
            binding.error_message,
        )
        self.assertEqual(adjustment.product_uom_qty, 0)
        self.assertAlmostEqual(sale.amount_total, 10.00)
        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, removed), binding.id
        )
        self.assertEqual(len(sale.order_line & adjustment), 1)

        restored = self._payload(
            order_id=613,
            total="10.01",
            updatedAt="2026-01-12T10:00:00Z",
            displayFinancialStatus="PAID",
        )
        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, restored),
            binding.id,
            binding.error_message,
        )
        self.assertEqual(adjustment.product_uom_qty, 1)
        self.assertAlmostEqual(sale.amount_total, 10.01)

    def test_confirmed_auxiliary_cleanup_does_not_unlink(self):
        binding = self._import_confirmed_two_line_order(615)
        sale = binding.odoo_id
        duty = (
            self.env["sale.order.line"]
            .with_context(shopify_order_import=True)
            .create(
                {
                    "order_id": sale.id,
                    "product_id": self.product.id,
                    "name": "Obsolete duty",
                    "product_uom_qty": 1,
                    "price_unit": 2,
                    "shopify_expected_total": "2.00",
                    "shopify_is_duty": True,
                }
            )
        )
        with patch.object(
            type(self.env["sale.order.line"]),
            "unlink",
            side_effect=AssertionError("Confirmed line must not be unlinked"),
        ):
            self.order_model._remove_auxiliary_lines(sale, duty)
        self.assertTrue(duty.exists())
        self.assertEqual(duty.product_uom_qty, 0)

    def test_confirmed_order_zeroes_duplicate_duty_lines(self):
        binding = self._import_confirmed_two_line_order(616)
        sale = binding.odoo_id
        self.instance._ensure_duties_product()
        first = (
            self.env["sale.order.line"]
            .with_context(shopify_order_import=True)
            .create(
                {
                    "order_id": sale.id,
                    "product_id": self.instance.duties_product_id.id,
                    "name": "Legacy duty",
                    "product_uom_qty": 1,
                    "price_unit": 1,
                    "shopify_is_duty": True,
                }
            )
        )
        duplicate = first.with_context(shopify_order_import=True).copy(
            default={"order_id": sale.id, "shopify_is_duty": True}
        )
        self.assertEqual(len(sale.order_line.filtered("shopify_is_duty")), 2)
        payload = self._two_line_payload(
            616,
            currentTotalDutiesSet=self._bag("2.00"),
            displayFinancialStatus="PAID",
            updatedAt="2026-01-12T10:00:00Z",
        )
        # The two-line fixture sets 20.00 before adding Shopify duties.
        payload["currentTotalPriceSet"] = self._bag("22.00")
        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, payload),
            binding.id,
            binding.error_message,
        )
        duty_lines = sale.order_line.filtered("shopify_is_duty")
        self.assertEqual(len(duty_lines), 2)
        self.assertEqual(first.product_uom_qty, 1)
        self.assertEqual(duplicate.product_uom_qty, 0)
        self.assertAlmostEqual(sale.amount_total, 22.00)

    def test_confirmed_order_zeroes_duplicate_rounding_lines(self):
        self.instance.order_confirmation_policy = "auto"
        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id,
                self._payload(
                    order_id=617,
                    total="10.01",
                    displayFinancialStatus="PAID",
                ),
            )
        )
        sale = binding.odoo_id
        adjustment = sale.order_line.filtered(
            lambda line: line.shopify_line_id
            == "shopify-subtotal-adjustment:gid://shopify/Order/617"
        )
        self.assertEqual(len(adjustment), 1)
        duplicate = adjustment.with_context(shopify_order_import=True).copy(
            default={
                "order_id": sale.id,
                "shopify_line_id": adjustment.shopify_line_id,
            }
        )
        payload = self._payload(
            order_id=617,
            total="10.01",
            displayFinancialStatus="PAID",
            updatedAt="2026-01-12T10:00:00Z",
        )
        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, payload),
            binding.id,
            binding.error_message,
        )
        self.assertTrue(duplicate.exists())
        self.assertEqual(duplicate.product_uom_qty, 0)
        self.assertEqual(
            len(
                sale.order_line.filtered(
                    lambda line: line.shopify_line_id
                    == "shopify-subtotal-adjustment:gid://shopify/Order/617"
                )
            ),
            2,
        )
        self.assertAlmostEqual(sale.amount_total, 10.01)

    def test_draft_order_removes_obsolete_duty_line(self):
        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id,
                self._payload(
                    order_id=614,
                    total="12.00",
                    currentTotalDutiesSet=self._bag("2.00"),
                ),
            )
        )
        sale = binding.odoo_id
        duty = sale.order_line.filtered("shopify_is_duty")
        self.assertEqual(sale.state, "draft")
        self.assertEqual(len(duty), 1)
        removed = self._payload(
            order_id=614, total="10.00", updatedAt="2026-01-11T10:00:00Z"
        )
        self.assertEqual(
            self.order_model._job_import_order(self.instance.id, removed),
            binding.id,
            binding.error_message,
        )
        self.assertFalse(duty.exists())
        self.assertFalse(sale.order_line.filtered("shopify_is_duty"))
        self.assertAlmostEqual(sale.amount_total, 10.00)

    def test_delivered_order_shipping_does_not_count_as_product_edit(self):
        shipping = {
            "id": "gid://shopify/ShippingLine/330",
            "title": "Standard shipping",
            "originalPriceSet": self._bag("0.00"),
            "discountedPriceSet": self._bag("0.00"),
            "taxLines": [],
        }
        payload = self._payload(
            order_id=330,
            shippingLines={"nodes": [shipping]},
        )
        binding_id = self.order_model._job_import_order(self.instance.id, payload)
        binding = self.order_model.browse(binding_id)

        self.assertEqual(len(binding.line_binding_ids.filtered("is_shipping")), 1)
        self.assertEqual(
            self.order_model._existing_line_snapshot(binding, binding.odoo_id),
            [{"id": "gid://shopify/LineItem/30", "quantity": 1}],
        )

        with patch.object(type(self.order_model), "_is_delivered", return_value=True):
            result = self.order_model._job_import_order(
                self.instance.id,
                self._payload(
                    order_id=330,
                    shippingLines={"nodes": [shipping]},
                    updatedAt="2026-01-12T10:00:00Z",
                ),
            )

        self.assertEqual(result, binding.id)
        self.assertEqual(binding.state, "synced")
        self.assertEqual(len(binding.line_binding_ids.filtered("is_shipping")), 1)

    def test_delivered_order_with_shipping_still_rejects_quantity_edit(self):
        shipping = {
            "id": "gid://shopify/ShippingLine/331",
            "title": "Standard shipping",
            "originalPriceSet": self._bag("0.00"),
            "discountedPriceSet": self._bag("0.00"),
            "taxLines": [],
        }
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(order_id=331, shippingLines={"nodes": [shipping]}),
        )
        binding = self.order_model.browse(binding_id)

        with patch.object(type(self.order_model), "_is_delivered", return_value=True):
            result = self.order_model._job_import_order(
                self.instance.id,
                self._payload(
                    order_id=331,
                    shippingLines={"nodes": [shipping]},
                    quantity=2,
                    total="20.00",
                    updatedAt="2026-01-12T10:00:00Z",
                ),
            )

        self.assertFalse(result)
        self.assertEqual(binding.state, "error")
        self.assertEqual(
            binding.odoo_id.order_line.filtered(
                lambda line: not line.shopify_is_shipping
            ).product_uom_qty,
            1,
        )

    def test_order_edit_after_delivery_is_rejected(self):
        binding_id = self.order_model._job_import_order(
            self.instance.id, self._payload(order_id=3)
        )
        binding = self.order_model.browse(binding_id)

        with patch.object(type(self.order_model), "_is_delivered", return_value=True):
            result = self.order_model._job_import_order(
                self.instance.id,
                self._payload(
                    order_id=3,
                    quantity=2,
                    total="20.00",
                    updatedAt="2026-01-12T10:00:00Z",
                ),
            )

        self.assertFalse(result)
        self.assertEqual(binding.state, "error")
        self.assertEqual(binding.odoo_id.order_line.product_uom_qty, 1)

    def test_cancel_after_delivery_is_rejected(self):
        binding_id = self.order_model._job_import_order(
            self.instance.id, self._payload(order_id=4)
        )
        binding = self.order_model.browse(binding_id)

        with patch.object(type(self.order_model), "_is_delivered", return_value=True):
            result = self.order_model._job_import_order(
                self.instance.id,
                self._cancelled_payload(order_id=4),
            )

        self.assertFalse(result)
        self.assertNotEqual(binding.odoo_id.state, "cancel")
        self.assertIn("already delivered", binding.error_message)

    def test_cancel_that_odoo_does_not_apply_is_reported(self):
        self.instance.order_confirmation_policy = "auto"
        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id,
                self._payload(order_id=13, displayFinancialStatus="PAID"),
            )
        )
        sale_order = binding.odoo_id

        # The cancel is blocked: Odoo returns an action and leaves the order.
        with patch.object(
            type(sale_order), "action_cancel", return_value={"type": "ir.actions"}
        ):
            result = self.order_model._job_import_order(
                self.instance.id,
                self._cancelled_payload(order_id=13, displayFinancialStatus="PAID"),
            )

        self.assertFalse(result)
        self.assertEqual(sale_order.state, "sale")
        self.assertIn("Odoo could not cancel", binding.error_message)

    def test_cancel_of_confirmed_order_cancels_the_sale_order(self):
        self.instance.order_confirmation_policy = "auto"
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(order_id=12, displayFinancialStatus="PAID"),
        )
        binding = self.order_model.browse(binding_id)
        self.assertEqual(binding.odoo_id.state, "sale")

        sale_order = binding.odoo_id

        result = self.order_model._job_import_order(
            self.instance.id,
            self._cancelled_payload(order_id=12, displayFinancialStatus="PAID"),
        )

        self.assertTrue(result, binding.error_message)
        self.assertEqual(sale_order.state, "cancel")
        self.assertEqual(sale_order.order_line.product_uom_qty, 1)

    def test_cancel_of_quotation_keeps_its_lines(self):
        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id, self._payload(order_id=15)
            )
        )
        sale_order = binding.odoo_id
        self.assertEqual(sale_order.state, "draft")

        result = self.order_model._job_import_order(
            self.instance.id, self._cancelled_payload(order_id=15)
        )

        self.assertTrue(result, binding.error_message)
        self.assertEqual(sale_order.state, "cancel")
        self.assertEqual(sale_order.order_line.product_uom_qty, 1)
        self.assertEqual(sale_order.amount_total, 10)

    def _two_line_payload(self, order_id, second_quantity=1, **values):
        """Return an order with a second line of a stored product.

        Without ``second_quantity`` the second line is not sent, as after
        Shopify removed it in an order edit.
        """
        quantity = second_quantity or 0
        payload = self._payload(
            order_id=order_id, total=f"{10 + quantity * 10}.00", **values
        )
        if second_quantity is None:
            return payload
        second = dict(payload["lineItems"]["nodes"][0])
        second.update(
            {
                "id": f"gid://shopify/LineItem/{order_id}31",
                "name": "Bound Goods Product",
                "sku": "ORDER-2",
                "quantity": 1,
                "currentQuantity": quantity,
                "refundableQuantity": quantity,
                "variant": {"id": self.goods_variant_binding.shopify_id},
            }
        )
        payload["lineItems"]["nodes"].append(second)
        return payload

    def _import_confirmed_two_line_order(self, order_id):
        self.instance.order_confirmation_policy = "auto"
        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id,
                self._two_line_payload(order_id, displayFinancialStatus="PAID"),
            )
        )
        self.assertEqual(binding.state, "synced", binding.error_message)
        self.assertEqual(binding.odoo_id.state, "sale")
        self.assertEqual(binding.odoo_id.amount_total, 20)
        return binding

    def test_refunded_line_of_confirmed_order_keeps_quantity_zero(self):
        binding = self._import_confirmed_two_line_order(16)
        sale_order = binding.odoo_id
        goods_line = sale_order.order_line.filtered(
            lambda line: line.product_id == self.goods
        )
        self.assertEqual(goods_line.move_ids.product_uom_qty, 1)

        result = self.order_model._job_import_order(
            self.instance.id,
            self._two_line_payload(
                16,
                second_quantity=0,
                displayFinancialStatus="PARTIALLY_REFUNDED",
                updatedAt="2026-01-11T10:00:00Z",
            ),
        )

        self.assertTrue(result, binding.error_message)
        self.assertEqual(binding.state, "synced", binding.error_message)
        self.assertEqual(len(sale_order.order_line), 2)
        self.assertEqual(goods_line.product_uom_qty, 0)
        self.assertEqual(len(binding.line_binding_ids), 2)
        self.assertEqual(sale_order.amount_total, 10)
        self.assertEqual(sum(goods_line.move_ids.mapped("product_uom_qty")), 0)

    def test_removed_line_of_confirmed_order_keeps_quantity_zero(self):
        binding = self._import_confirmed_two_line_order(17)
        sale_order = binding.odoo_id
        goods_line = sale_order.order_line.filtered(
            lambda line: line.product_id == self.goods
        )

        result = self.order_model._job_import_order(
            self.instance.id,
            self._two_line_payload(
                17,
                second_quantity=None,
                displayFinancialStatus="PAID",
                updatedAt="2026-01-11T10:00:00Z",
            ),
        )

        self.assertTrue(result, binding.error_message)
        self.assertEqual(binding.state, "synced", binding.error_message)
        self.assertEqual(len(sale_order.order_line), 2)
        self.assertEqual(goods_line.product_uom_qty, 0)
        self.assertEqual(len(binding.line_binding_ids), 2)
        self.assertEqual(sale_order.amount_total, 10)

    def test_removed_line_of_quotation_is_deleted(self):
        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id, self._two_line_payload(18)
            )
        )
        sale_order = binding.odoo_id
        self.assertEqual(sale_order.state, "draft")
        self.assertEqual(len(sale_order.order_line), 2)

        result = self.order_model._job_import_order(
            self.instance.id,
            self._two_line_payload(
                18, second_quantity=None, updatedAt="2026-01-11T10:00:00Z"
            ),
        )

        self.assertTrue(result, binding.error_message)
        self.assertEqual(len(sale_order.order_line), 1)
        self.assertEqual(len(binding.line_binding_ids), 1)
        self.assertEqual(sale_order.amount_total, 10)

    def test_unmapped_tax_records_actionable_error(self):
        payload = self._payload(order_id=5, total="11.00")
        payload["shippingAddress"] = {"countryCodeV2": "US"}
        payload["currentTotalTaxSet"] = self._bag("1.00")
        payload["lineItems"]["nodes"][0]["taxLines"] = [
            {"title": "Tax", "rate": "0.1", "priceSet": self._bag("1.00")}
        ]

        result = self.order_model._job_import_order(self.instance.id, payload)

        binding = self.order_model.search(
            [
                ("instance_id", "=", self.instance.id),
                ("shopify_id", "=", "gid://shopify/Order/5"),
            ]
        )
        self.assertFalse(result)
        self.assertEqual(binding.missing_tax_rate, "0.1")
        self.assertTrue(binding.missing_tax_country_id)

    def test_zero_amount_tax_line_does_not_require_tax_mapping(self):
        # SW-OCA-009: mirror Shopify's zero tax rather than applying a
        # nonzero Odoo tax solely because a rate was included in the payload.
        payload = self._payload(order_id=94, total="10.00")
        payload["shippingAddress"] = {"countryCodeV2": "US"}
        payload["lineItems"]["nodes"][0]["taxLines"] = [
            {"title": "Zero amount tax", "rate": "0.1", "priceSet": self._bag("0.00")}
        ]

        binding_id = self.order_model._job_import_order(self.instance.id, payload)

        self.assertTrue(binding_id)
        binding = self.order_model.browse(binding_id)
        self.assertEqual(binding.state, "synced")
        self.assertEqual(binding.odoo_id.amount_total, 10.0)
        self.assertFalse(binding.odoo_id.order_line.tax_ids)

    def _austin_payload(self, order_id):
        # Austin, TX: state 6.25 %, city 1 %, transit 1 %.
        payload = self._payload(order_id=order_id, total="108.25")
        payload["shippingAddress"] = {"countryCodeV2": "US"}
        payload["currentSubtotalPriceSet"] = self._bag("100.00")
        payload["currentTotalTaxSet"] = self._bag("8.25")
        line = payload["lineItems"]["nodes"][0]
        line["id"] = f"gid://shopify/LineItem/{order_id}0"
        line["originalUnitPriceSet"] = self._bag("100.00")
        line["discountedUnitPriceAfterAllDiscountsSet"] = self._bag("100.00")
        line["taxLines"] = [
            {"title": title, "rate": rate, "priceSet": self._bag(amount)}
            for title, rate, amount in (
                ("Texas State Tax", "0.0625", "6.25"),
                ("Austin City Tax", "0.01", "1.00"),
                ("Austin MTA Transit", "0.01", "1.00"),
            )
        ]
        return payload

    def _map_tax(self, rate, amount):
        tax = self.env["account.tax"].create(
            {
                "name": f"Tax {amount}%",
                "amount": amount,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": self.instance.company_id.id,
            }
        )
        self.env["shopify.tax.mapping"].create(
            {
                "instance_id": self.instance.id,
                "rate": rate,
                "country_id": self.env.ref("base.us").id,
                "price_included": False,
                "tax_id": tax.id,
            }
        )
        return tax

    def test_same_rate_tax_lines_use_the_mapping_of_their_sum(self):
        state_tax = self._map_tax("0.0625", 6.25)
        self._map_tax("0.01", 1.0)
        local_tax = self._map_tax("0.02", 2.0)

        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id, self._austin_payload(order_id=92)
            )
        )

        self.assertEqual(binding.state, "synced")
        self.assertEqual(binding.odoo_id.amount_total, 108.25)
        self.assertEqual(binding.odoo_id.order_line.tax_ids, state_tax | local_tax)

    def test_same_rate_tax_lines_ask_for_the_mapping_of_their_sum(self):
        self._map_tax("0.0625", 6.25)
        self._map_tax("0.01", 1.0)

        result = self.order_model._job_import_order(
            self.instance.id, self._austin_payload(order_id=93)
        )

        binding = self.order_model.search(
            [
                ("instance_id", "=", self.instance.id),
                ("shopify_id", "=", "gid://shopify/Order/93"),
            ]
        )
        self.assertFalse(result)
        self.assertEqual(binding.missing_tax_rate, "0.02")

    def test_high_risk_sets_hold_and_release_action(self):
        payload = self._payload(order_id=6)
        payload["risk"] = {"assessments": [{"riskLevel": "HIGH"}]}

        binding_id = self.order_model._job_import_order(self.instance.id, payload)
        order = self.order_model.browse(binding_id).odoo_id

        self.assertTrue(order.shopify_risk_hold)
        order.action_release_shopify_risk_hold()
        self.assertFalse(order.shopify_risk_hold)

    def test_sale_order_takes_the_shopify_order_name(self):
        payload = self._payload(order_id=16)
        payload["lineItems"]["nodes"][0]["id"] = "gid://shopify/LineItem/160"
        existing = self.order_model.browse(
            self.order_model._job_import_order(self.instance.id, payload)
        )
        odoo_name = existing.odoo_id.name
        self.instance.order_use_shopify_name = True

        payload["updatedAt"] = "2026-01-11T10:00:00Z"
        self.order_model._job_import_order(self.instance.id, payload)
        draft = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id, self._draft_payload(), is_draft=True
            )
        )
        self.assertEqual(draft.odoo_id.name, "#D90")
        order = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id,
                self._payload(order_id=90, sourceName="shopify_draft_order"),
            )
        )

        self.assertEqual(existing.odoo_id.name, odoo_name)
        self.assertEqual(order.odoo_id, draft.odoo_id)
        self.assertEqual(order.odoo_id.name, "#90")

    def _draft_payload(self, draft_id=90, order_id=90):
        return {
            "id": f"gid://shopify/DraftOrder/{draft_id}",
            "name": f"#D{draft_id}",
            "status": "COMPLETED",
            "createdAt": "2026-01-10T10:00:00Z",
            "updatedAt": "2026-01-10T10:00:00Z",
            "currencyCode": self.currency.name,
            "presentmentCurrencyCode": self.currency.name,
            "subtotalPriceSet": self._bag("10.00"),
            "totalDiscountsSet": self._bag("0.00"),
            "totalShippingPriceSet": self._bag("0.00"),
            "totalTaxSet": self._bag("0.00"),
            "totalPriceSet": self._bag("10.00"),
            "order": {"id": f"gid://shopify/Order/{order_id}"},
            "lineItems": {
                "nodes": [
                    {
                        "id": "gid://shopify/DraftOrderLineItem/900",
                        "name": "Bound Order Product",
                        "quantity": 1,
                        "variant": {"id": self.variant_binding.shopify_id},
                        "originalUnitPriceSet": self._bag("10.00"),
                        "discountedTotalSet": self._bag("10.00"),
                        "taxLines": [],
                    }
                ]
            },
        }

    def test_draft_order_discount_reaches_the_sale_order(self):
        payload = self._draft_payload(draft_id=91, order_id=91)
        payload.update(
            {
                "status": "OPEN",
                "order": None,
                "appliedDiscount": {"title": "Test", "amountSet": self._bag("1.00")},
                "subtotalPriceSet": self._bag("9.00"),
                "totalDiscountsSet": self._bag("1.00"),
                "totalPriceSet": self._bag("9.00"),
            }
        )

        binding = self.order_model.browse(
            self.order_model._job_import_order(self.instance.id, payload, is_draft=True)
        )

        self.assertEqual(binding.state, "synced", binding.error_message)
        self.assertEqual(binding.odoo_id.amount_total, 9.0)
        self.assertEqual(binding.odoo_id.order_line.discount, 10.0)

    def test_completed_draft_links_real_order_without_duplicate_sale(self):
        draft = self._draft_payload()
        before = self.env["sale.order"].search_count([])
        draft_binding_id = self.order_model._job_import_order(
            self.instance.id, draft, is_draft=True
        )

        order_binding_id = self.order_model._job_import_order(
            self.instance.id, self._payload(order_id=90)
        )

        draft_binding = self.order_model.browse(draft_binding_id)
        order_binding = self.order_model.browse(order_binding_id)
        self.assertEqual(self.env["sale.order"].search_count([]), before + 1)
        self.assertEqual(draft_binding.odoo_id, order_binding.odoo_id)
        self.assertEqual(draft_binding.replaced_by_id, order_binding)
        self.assertEqual(len(order_binding.odoo_id.order_line), 1)

    def test_order_from_draft_waits_for_its_draft_order(self):
        with self.assertRaises(RetryableJobError):
            self.order_model._job_import_order(
                self.instance.id,
                self._payload(order_id=91, sourceName="shopify_draft_order"),
            )
        self.assertFalse(
            self.order_model.search([("shopify_id", "=", "gid://shopify/Order/91")])
        )

    def test_order_from_draft_is_imported_alone_on_its_last_attempt(self):
        job = self.order_model.with_delay()._job_import_order(
            self.instance.id,
            self._payload(order_id=91, sourceName="shopify_draft_order"),
        )
        for _attempt in range(job.max_retries - 1):
            with self.assertRaises(RetryableJobError):
                job.perform()
            job.store()

        binding = self.order_model.browse(job.perform())

        self.assertEqual(binding.state, "synced")
        self.assertEqual(binding.odoo_id.amount_total, 10)
        self.assertTrue(
            self.env["shopify.log"].search(
                [
                    ("res_model", "=", binding._name),
                    ("res_id", "=", binding.id),
                    ("level", "=", "warning"),
                ]
            )
        )

    def test_order_from_draft_takes_over_confirmed_draft_quotation(self):
        draft_binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id, self._draft_payload(), is_draft=True
            )
        )
        quotation = draft_binding.odoo_id
        draft_line = quotation.order_line
        quotation.action_confirm()

        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id,
                self._payload(order_id=90, sourceName="shopify_draft_order"),
            )
        )

        self.assertEqual(binding.state, "synced")
        self.assertEqual(binding.odoo_id, quotation)
        self.assertEqual(quotation.order_line, draft_line)
        self.assertEqual(binding.line_binding_ids.odoo_id, draft_line)
        self.assertEqual(draft_line.shopify_line_id, "gid://shopify/LineItem/30")
        self.assertEqual(draft_binding.replaced_by_id, binding)

    def test_order_from_draft_is_imported_alone_when_drafts_are_disabled(self):
        self.instance.draft_order_import_enabled = False

        draft_binding_id = self.order_model._job_import_order(
            self.instance.id, self._draft_payload(), is_draft=True
        )
        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id,
                self._payload(order_id=90, sourceName="shopify_draft_order"),
            )
        )

        self.assertFalse(draft_binding_id)
        self.assertEqual(binding.odoo_id.amount_total, 10)
        self.assertNotIn(
            "DRAFT_ORDERS_CREATE", self.instance._required_webhook_topics()
        )

    def test_order_import_is_retried_while_another_job_holds_the_order(self):
        with db_connect(self.env.cr.dbname).cursor() as cr:
            cr.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                (self.instance.id, "gid://shopify/Order/1"),
            )
            with self.assertRaises(RetryableJobError):
                self.order_model._job_import_order(self.instance.id, self._payload())

    def test_gateway_mapping_is_case_normalized_for_payment_split(self):
        journal = self.bank_journal
        if not journal:
            self.skipTest("No bank or cash journal is installed for the test company.")

        mapping = self.env["shopify.gateway.journal"].create(
            {
                "instance_id": self.instance.id,
                "gateway": "  Shop Pay ",
                "journal_id": journal.id,
            }
        )

        self.assertEqual(mapping.gateway, "shop pay")

    def test_paid_order_registers_each_gateway_transaction_once(self):
        journal = self.bank_journal
        sale_journal = self.sale_journal
        if not journal or not sale_journal:
            self.skipTest("The test company has no accounting journals.")
        self.instance.write(
            {
                "order_confirmation_policy": "auto",
                "order_invoicing_policy": "paid",
            }
        )
        for gateway in ("gateway-a", "gateway-b"):
            self.env["shopify.gateway.journal"].create(
                {
                    "instance_id": self.instance.id,
                    "gateway": gateway,
                    "journal_id": journal.id,
                }
            )
        payload = self._payload(order_id=7, displayFinancialStatus="PAID")
        payload["transactions"] = [
            {
                "id": "gid://shopify/OrderTransaction/701",
                "gateway": "gateway-a",
                "kind": "SALE",
                "status": "SUCCESS",
                "amountSet": self._bag("4.00"),
            },
            {
                "id": "gid://shopify/OrderTransaction/702",
                "gateway": "gateway-b",
                "kind": "CAPTURE",
                "status": "SUCCESS",
                "amountSet": self._bag("6.00"),
            },
        ]

        binding_id = self.order_model._job_import_order(self.instance.id, payload)
        self.order_model._job_import_order(self.instance.id, payload)

        payments = self.env["shopify.gateway.payment"].search(
            [("order_binding_id", "=", binding_id)]
        )
        self.assertEqual(len(payments), 2)
        self.assertEqual(set(payments.mapped("amount")), {"4.00", "6.00"})

    def test_refund_of_invoiced_order_creates_posted_credit_note(self):
        sale_journal = self.sale_journal
        if not sale_journal:
            self.skipTest("The test company has no sales journal.")
        self.instance.write(
            {
                "order_confirmation_policy": "auto",
                "order_invoicing_policy": "paid",
            }
        )
        initial = self._payload(order_id=8, displayFinancialStatus="PAID")
        binding_id = self.order_model._job_import_order(self.instance.id, initial)
        binding = self.order_model.browse(binding_id)
        self.assertTrue(binding.odoo_id.invoice_ids)

        refunded = self._payload(
            order_id=8,
            displayFinancialStatus="PAID",
            updatedAt="2026-01-13T10:00:00Z",
        )
        refunded["refunds"] = {
            "nodes": [
                {
                    "id": "gid://shopify/Refund/801",
                    "createdAt": "2026-01-13T10:00:00Z",
                    "totalRefundedSet": self._bag("5.00"),
                    "refundLineItems": {
                        "nodes": [
                            {
                                "id": "gid://shopify/RefundLineItem/802",
                                "lineItem": {"id": "gid://shopify/LineItem/30"},
                                "quantity": 1,
                                "restockType": "NO_RESTOCK",
                                "subtotalSet": self._bag("5.00"),
                                "totalTaxSet": self._bag("0.00"),
                            }
                        ]
                    },
                    "refundShippingLines": {"nodes": []},
                    "orderAdjustments": {"nodes": []},
                    "transactions": {"nodes": []},
                }
            ]
        }

        result = self.order_model._job_import_order(self.instance.id, refunded)

        refund = binding.refund_binding_ids
        self.assertTrue(result, binding.error_message)
        self.assertEqual(len(refund), 1)
        self.assertEqual(refund.move_id.move_type, "out_refund")
        self.assertEqual(refund.move_id.state, "posted")

    def test_updated_order_drift_pages_and_advances_cursor(self):
        self.instance.write({"order_drift_since": datetime(2026, 10, 1, 8)})
        client = Mock()
        client.execute.side_effect = [
            {
                "orders": {
                    "nodes": [
                        {
                            "id": "gid://shopify/Order/101",
                            "name": "#101",
                            "createdAt": "2026-10-10T10:00:00Z",
                            "updatedAt": "2026-10-10T10:00:00Z",
                        }
                    ],
                    "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                }
            },
            {
                "orders": {
                    "nodes": [
                        {
                            "id": "gid://shopify/Order/102",
                            "name": "#102",
                            "createdAt": "2026-10-10T10:00:00Z",
                            "updatedAt": "2026-10-10T10:00:00Z",
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                }
            },
        ]
        with (
            freeze_time("2026-11-02 09:00:00"),
            trap_jobs() as trap,
            patch.object(type(self.instance), "_shopify_client", return_value=client),
        ):
            count = self.instance._job_sync_updated_orders()
        self.assertEqual(count, 2)
        search = "updated_at:>='2026-10-01T07:00:00Z'"
        self.assertEqual(
            [call.args[1] for call in client.execute.call_args_list],
            [{"after": None, "query": search}, {"after": "next", "query": search}],
        )
        trap.assert_jobs_count(2, only=self.order_model._job_import_order_from_api)
        self.assertEqual(self.instance.order_drift_since, datetime(2026, 11, 2, 9))

    def test_updated_order_drift_skips_current_bindings(self):
        self.order_model.create(
            {
                "instance_id": self.instance.id,
                "shopify_id": "gid://shopify/Order/101",
                "order_name": "#101",
                "external_updated_at": datetime(2026, 11, 1, 10),
                "state": "synced",
            }
        )
        client = Mock()
        client.execute.return_value = {
            "orders": {
                "nodes": [
                    {
                        "id": "gid://shopify/Order/101",
                        "name": "#101",
                        "updatedAt": "2026-10-10T10:00:00Z",
                    },
                    {
                        "id": "gid://shopify/Order/102",
                        "name": "#102",
                        "createdAt": "2026-11-02T10:00:00Z",
                        "updatedAt": "2026-11-02T10:00:00Z",
                    },
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
        with (
            trap_jobs() as trap,
            patch.object(type(self.instance), "_shopify_client", return_value=client),
        ):
            count = self.instance._job_sync_updated_orders()
        self.assertEqual(count, 1)
        trap.assert_enqueued_job(
            self.order_model._job_import_order_from_api,
            args=(self.instance.id, "gid://shopify/Order/102"),
        )

    def test_updated_order_drift_does_not_advance_on_api_error(self):
        original = datetime(2026, 10, 1, 8)
        self.instance.write({"order_drift_since": original})
        client = Mock()
        client.execute.side_effect = ShopifyError("Shopify unavailable")
        with (
            patch.object(type(self.instance), "_shopify_client", return_value=client),
            self.assertRaises(ShopifyError),
        ):
            self.instance._job_sync_updated_orders()
        self.assertEqual(self.instance.order_drift_since, original)

    def test_drift_first_run_scans_two_days_with_overlap(self):
        # SW-OCA-003: preserve the upstream initial window, plus a one-hour
        # overlap; the pending ledger makes subsequent retries durable.
        self.instance.write(
            {
                "order_drift_since": False,
                "order_drift_pending": {},
                "draft_order_import_enabled": False,
            }
        )
        client = Mock()
        client.execute.return_value = {
            "orders": {"nodes": [], "pageInfo": {"hasNextPage": False}}
        }
        with (
            freeze_time("2026-11-02 09:00:00"),
            patch.object(type(self.instance), "_shopify_client", return_value=client),
        ):
            self.assertEqual(self.instance._job_sync_updated_orders(), 0)
        self.assertEqual(
            client.execute.call_args.args[1]["query"],
            "updated_at:>='2026-10-31T08:00:00Z'",
        )
        self.assertEqual(self.instance.order_drift_since, datetime(2026, 11, 2, 9))

    def test_drift_cron_queues_reconciliation(self):
        # SW-OCA-003: normal scheduling must keep using the pending ledger.
        with trap_jobs() as trap:
            self.env["shopify.instance"]._cron_reconcile_orders()
        trap.assert_jobs_count(1, only=self.instance._job_sync_updated_orders)

    def test_drift_search_converts_timezone_to_utc(self):
        # Preserve upstream query semantics independently of the ledger.
        since = datetime(
            2026, 11, 2, 10, 30, 15, 500, tzinfo=timezone(timedelta(hours=1))
        )
        self.assertEqual(
            orders_updated_search(since),
            "updated_at:>='2026-11-02T09:30:15Z'",
        )

    def test_order_queries_request_full_upstream_addresses(self):
        self.assertEqual(
            set(ADDRESS_FIELDS.split()),
            {
                "firstName",
                "lastName",
                "company",
                "address1",
                "address2",
                "city",
                "provinceCode",
                "countryCodeV2",
                "zip",
                "phone",
            },
        )
        for query in (
            ORDER_BY_ID_QUERY,
            DRAFT_ORDER_BY_ID_QUERY,
            DRAFT_ORDERS_QUERY,
        ):
            with self.subTest(query=query[:50]):
                self.assertIn(f"shippingAddress {{ {ADDRESS_FIELDS} }}", query)
                self.assertIn(f"billingAddress {{ {ADDRESS_FIELDS} }}", query)

    def test_new_order_uses_bound_customer_and_full_addresses(self):
        customer_id = "gid://shopify/Customer/117"
        customer_partner = self.env["res.partner"].create(
            {"name": "Bound Customer", "company_id": self.instance.company_id.id}
        )
        self.env["shopify.customer"].create(
            {
                "instance_id": self.instance.id,
                "shopify_id": customer_id,
                "odoo_id": customer_partner.id,
            }
        )
        shipping = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "address1": "Delivery Road",
            "city": "Vienna",
            "zip": "1010",
            "countryCodeV2": "AT",
        }
        billing = {**shipping, "address1": "Invoice Road"}
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=117,
                customer={"id": customer_id},
                shippingAddress=shipping,
                billingAddress=billing,
            ),
        )
        sale = self.order_model.browse(binding_id).odoo_id
        self.assertEqual(sale.partner_id, customer_partner)
        self.assertEqual(sale.partner_shipping_id.parent_id, customer_partner)
        self.assertEqual(sale.partner_invoice_id.parent_id, customer_partner)
        self.assertEqual(sale.partner_shipping_id.street, "Delivery Road")
        self.assertEqual(sale.partner_invoice_id.street, "Invoice Road")

    def test_order_import_uses_distinct_shipping_and_invoice_addresses(self):
        shipping = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "address1": "Shipping 1",
            "city": "Vienna",
            "zip": "1010",
            "countryCodeV2": "AT",
        }
        billing = {**shipping, "address1": "Billing 2"}
        payload = self._payload(
            order_id=109, shippingAddress=shipping, billingAddress=billing
        )
        binding_id = self.order_model._job_import_order(self.instance.id, payload)
        order = self.order_model.browse(binding_id).odoo_id
        self.assertEqual(order.partner_shipping_id.street, "Shipping 1")
        self.assertEqual(order.partner_invoice_id.street, "Billing 2")
        self.assertNotEqual(order.partner_shipping_id, order.partner_invoice_id)

    def test_reimported_order_does_not_duplicate_address_contacts(self):
        address = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "address1": "Shipping 1",
            "city": "Vienna",
            "zip": "1010",
            "countryCodeV2": "AT",
        }
        payload = self._payload(
            order_id=110, shippingAddress=address, billingAddress=address
        )
        binding_id = self.order_model._job_import_order(self.instance.id, payload)
        order = self.order_model.browse(binding_id).odoo_id
        before = order.partner_id.child_ids
        payload["updatedAt"] = "2026-01-11T10:00:00Z"
        self.order_model._job_import_order(self.instance.id, payload)
        self.assertEqual(order.partner_id.child_ids, before)
        self.assertEqual(order.partner_shipping_id, order.partner_invoice_id)

    def test_cancelled_confirmed_order_keeps_lines(self):
        self.instance.order_confirmation_policy = "auto"
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(order_id=111, displayFinancialStatus="PAID"),
        )
        order = self.order_model.browse(binding_id).odoo_id
        self.assertEqual(order.state, "sale")
        payload = self._payload(
            order_id=111,
            quantity=0,
            total="0.00",
            cancelledAt="2026-01-12T10:00:00Z",
            updatedAt="2026-01-12T10:00:00Z",
        )
        self.assertTrue(self.order_model._job_import_order(self.instance.id, payload))
        self.assertEqual(order.state, "cancel")
        self.assertEqual(order.order_line.product_uom_qty, 1)

    def test_drift_pending_survives_failed_and_never_executed_child_jobs(self):
        gid = "gid://shopify/Order/501"
        stamp = "2026-10-25T10:00:00Z"
        self.instance.order_drift_since = datetime(2026, 10, 1, 8)
        client = Mock()
        client.execute.return_value = {
            "orders": {
                "nodes": [
                    {
                        "id": gid,
                        "name": "#501",
                        "createdAt": "2026-01-10T10:00:00Z",
                        "updatedAt": stamp,
                    }
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
        with (
            freeze_time("2026-11-02 09:00:00"),
            trap_jobs() as trap,
            patch.object(type(self.instance), "_shopify_client", return_value=client),
        ):
            self.assertEqual(self.instance._job_sync_updated_orders(), 1)
        trap.assert_enqueued_job(
            self.order_model._job_import_order_from_api,
            args=(self.instance.id, gid),
        )
        self.assertIn(gid, self.instance.order_drift_pending)

        # The child returns False after persisting an error binding. Its
        # update is older than the next daily scan's one-hour overlap.
        bad = self._payload(order_id=501, total="10.02", updatedAt=stamp)
        self.assertFalse(self.order_model._job_import_order(self.instance.id, bad))
        binding = self.order_model.search(
            [("instance_id", "=", self.instance.id), ("shopify_id", "=", gid)]
        )
        self.assertEqual(binding.state, "error")
        client.execute.return_value = {
            "orders": {"nodes": [], "pageInfo": {"hasNextPage": False}}
        }
        with (
            freeze_time("2026-11-05 09:00:00"),
            trap_jobs() as trap,
            patch.object(type(self.instance), "_shopify_client", return_value=client),
        ):
            self.assertEqual(self.instance._job_sync_updated_orders(), 1)
        trap.assert_enqueued_job(
            self.order_model._job_import_order_from_api,
            args=(self.instance.id, gid),
        )
        self.assertIn(gid, self.instance.order_drift_pending)

        # A repaired import acknowledges the version on the following scan.
        corrected = self._payload(order_id=501, updatedAt=stamp)
        self.assertTrue(self.order_model._job_import_order(self.instance.id, corrected))
        self.assertEqual(binding.state, "synced")
        with (
            freeze_time("2026-11-06 09:00:00"),
            trap_jobs() as trap,
            patch.object(type(self.instance), "_shopify_client", return_value=client),
        ):
            self.assertEqual(self.instance._job_sync_updated_orders(), 0)
        self.assertFalse(self.instance.order_drift_pending)
        trap.assert_jobs_count(0)

    def test_paid_historical_order_is_not_confirmed_by_auto_invoicing(self):
        self.instance.write(
            {
                "order_confirmation_policy": "auto",
                "order_invoicing_policy": "paid",
                "auto_confirm_order_date_from": datetime(2026, 2, 1),
            }
        )
        payload = self._payload(
            order_id=502,
            displayFinancialStatus="PAID",
            updatedAt="2026-11-05T10:00:00Z",
        )
        binding_id = self.order_model._job_import_order(self.instance.id, payload)
        sale = self.order_model.browse(binding_id).odoo_id
        self.assertEqual(sale.state, "draft")
        self.assertFalse(sale.invoice_ids)
        self.assertFalse(sale.picking_ids)

    def test_paid_recent_order_still_auto_invoices_after_cutoff(self):
        self.instance.write(
            {
                "order_confirmation_policy": "auto",
                "order_invoicing_policy": "paid",
                "auto_confirm_order_date_from": datetime(2026, 2, 1),
            }
        )
        payload = self._payload(
            order_id=503,
            createdAt="2026-02-02T10:00:00Z",
            updatedAt="2026-11-05T10:00:00Z",
            displayFinancialStatus="PAID",
        )
        binding_id = self.order_model._job_import_order(self.instance.id, payload)
        sale = self.order_model.browse(binding_id).odoo_id
        self.assertEqual(sale.state, "sale")
        self.assertTrue(sale.invoice_ids)
        self.assertTrue(all(i.state == "posted" for i in sale.invoice_ids))

    def test_confirmed_country_only_addresses_preserve_existing_contacts(self):
        self.instance.order_confirmation_policy = "auto"
        shipping = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "address1": "Shipping 1",
            "city": "Vienna",
            "zip": "1010",
            "countryCodeV2": "AT",
        }
        billing = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "address1": "Billing 2",
            "city": "Vienna",
            "zip": "1020",
            "countryCodeV2": "AT",
        }
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=507,
                shippingAddress=shipping,
                billingAddress=billing,
                displayFinancialStatus="PAID",
            ),
        )
        binding = self.order_model.browse(binding_id)
        sale = binding.odoo_id
        shipping_contact = sale.partner_shipping_id
        billing_contact = sale.partner_invoice_id
        self.assertEqual(sale.state, "sale")
        self.assertNotEqual(shipping_contact, billing_contact)
        self.assertNotEqual(shipping_contact, sale.partner_id)
        self.assertNotEqual(billing_contact, sale.partner_id)

        # Shopify Basic can expose the destination country but no address PII.
        result = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=507,
                shippingAddress={"countryCodeV2": "AT"},
                billingAddress={"countryCodeV2": "AT"},
                displayFinancialStatus="PAID",
                updatedAt="2026-01-12T10:00:00Z",
            ),
        )
        self.assertEqual(result, binding.id)
        self.assertEqual(binding.state, "synced")
        self.assertEqual(sale.partner_shipping_id, shipping_contact)
        self.assertEqual(sale.partner_invoice_id, billing_contact)

        # An entirely omitted address must not clear existing contacts either.
        result = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=507,
                displayFinancialStatus="PAID",
                updatedAt="2026-01-13T10:00:00Z",
            ),
        )
        self.assertEqual(result, binding.id)
        self.assertEqual(binding.state, "synced")
        self.assertEqual(sale.partner_shipping_id, shipping_contact)
        self.assertEqual(sale.partner_invoice_id, billing_contact)

    def test_confirmed_order_billing_address_drift_is_actionable(self):
        self.instance.order_confirmation_policy = "auto"
        original = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "address1": "Billing A",
            "city": "Vienna",
            "zip": "1010",
            "countryCodeV2": "AT",
        }
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=504,
                billingAddress=original,
                displayFinancialStatus="PAID",
            ),
        )
        binding = self.order_model.browse(binding_id)
        sale = binding.odoo_id
        old_partner = sale.partner_invoice_id
        self.assertEqual(sale.state, "sale")
        changed = {**original, "address1": "Billing B"}
        result = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=504,
                billingAddress=changed,
                updatedAt="2026-11-05T10:00:00Z",
                displayFinancialStatus="PAID",
            ),
        )
        self.assertFalse(result)
        self.assertEqual(binding.state, "error")
        self.assertIn("billing address differs", binding.error_message)
        self.assertEqual(sale.partner_invoice_id, old_partner)

    def test_confirmed_order_shipping_address_drift_is_actionable(self):
        self.instance.order_confirmation_policy = "auto"
        original = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "address1": "Shipping A",
            "city": "Vienna",
            "zip": "1010",
            "countryCodeV2": "AT",
        }
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=505,
                shippingAddress=original,
                displayFinancialStatus="PAID",
            ),
        )
        binding = self.order_model.browse(binding_id)
        sale = binding.odoo_id
        old_partner = sale.partner_shipping_id
        self.assertEqual(sale.state, "sale")
        changed = {**original, "address1": "Shipping B"}
        result = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=505,
                shippingAddress=changed,
                updatedAt="2026-11-05T10:00:00Z",
                displayFinancialStatus="PAID",
            ),
        )
        self.assertFalse(result)
        self.assertEqual(binding.state, "error")
        self.assertIn("shipping address differs", binding.error_message)
        self.assertEqual(sale.partner_shipping_id, old_partner)

    def test_confirmed_shipping_province_only_change_is_not_silently_synced(self):
        self.instance.order_confirmation_policy = "auto"
        original = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "address1": "123 Main St",
            "city": "Albany",
            "zip": "12207",
            "countryCodeV2": "US",
            "provinceCode": "NY",
            "phone": "+1-555-1111",
        }
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=506,
                shippingAddress=original,
                displayFinancialStatus="PAID",
            ),
        )
        binding = self.order_model.browse(binding_id)
        sale = binding.odoo_id
        self.assertEqual(sale.state, "sale")
        old_digest = binding.payload_digest
        old_partner = sale.partner_shipping_id
        old_state = old_partner.state_id
        self.assertTrue(old_state)
        changed = {**original, "provinceCode": "NJ"}
        result = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=506,
                shippingAddress=changed,
                displayFinancialStatus="PAID",
                updatedAt="2026-11-05T10:00:00Z",
            ),
        )
        self.assertFalse(result)
        self.assertEqual(binding.state, "error")
        self.assertIn("shipping address differs", binding.error_message)
        self.assertEqual(binding.payload_digest, old_digest)
        self.assertEqual(sale.partner_shipping_id, old_partner)
        self.assertEqual(old_partner.state_id, old_state)

    def test_confirmed_billing_phone_only_change_is_not_silently_synced(self):
        self.instance.order_confirmation_policy = "auto"
        original = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "address1": "123 Main St",
            "city": "Albany",
            "zip": "12207",
            "countryCodeV2": "US",
            "provinceCode": "NY",
            "phone": "+1-555-1111",
        }
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=507,
                billingAddress=original,
                displayFinancialStatus="PAID",
            ),
        )
        binding = self.order_model.browse(binding_id)
        sale = binding.odoo_id
        self.assertEqual(sale.state, "sale")
        old_digest = binding.payload_digest
        old_partner = sale.partner_invoice_id
        changed = {**original, "phone": "+1-555-2222"}
        result = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=507,
                billingAddress=changed,
                displayFinancialStatus="PAID",
                updatedAt="2026-11-05T10:00:00Z",
            ),
        )
        self.assertFalse(result)
        self.assertEqual(binding.state, "error")
        self.assertIn("billing address differs", binding.error_message)
        self.assertEqual(binding.payload_digest, old_digest)
        self.assertEqual(sale.partner_invoice_id, old_partner)
        self.assertEqual(old_partner.phone, original["phone"])

    def test_confirmed_billing_company_only_change_is_not_silently_synced(self):
        self.instance.order_confirmation_policy = "auto"
        original = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "company": "Company A",
            "address1": "123 Main St",
            "city": "Albany",
            "zip": "12207",
            "countryCodeV2": "US",
        }
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=508,
                billingAddress=original,
                displayFinancialStatus="PAID",
            ),
        )
        binding = self.order_model.browse(binding_id)
        sale = binding.odoo_id
        self.assertEqual(sale.state, "sale")
        old_partner = sale.partner_invoice_id
        self.assertEqual(old_partner.name, "Company A / Ada Lovelace")
        changed = {**original, "company": "Company B"}
        result = self.order_model._job_import_order(
            self.instance.id,
            self._payload(
                order_id=508,
                billingAddress=changed,
                displayFinancialStatus="PAID",
                updatedAt="2026-11-05T10:00:00Z",
            ),
        )
        self.assertFalse(result)
        self.assertEqual(binding.state, "error")
        self.assertEqual(sale.partner_invoice_id, old_partner)
        self.assertEqual(old_partner.name, "Company A / Ada Lovelace")

    def test_draft_shipping_phone_change_reuses_full_address_only(self):
        original = {
            "firstName": "Ada",
            "lastName": "Lovelace",
            "address1": "123 Main St",
            "city": "Albany",
            "zip": "12207",
            "countryCodeV2": "US",
            "phone": "+1-555-1111",
        }
        binding_id = self.order_model._job_import_order(
            self.instance.id,
            self._payload(order_id=509, shippingAddress=original),
        )
        binding = self.order_model.browse(binding_id)
        sale = binding.odoo_id
        first_contact = sale.partner_shipping_id
        updated = {**original, "phone": "+1-555-2222"}
        changed_payload = self._payload(
            order_id=509,
            shippingAddress=updated,
            updatedAt="2026-11-05T10:00:00Z",
        )
        self.assertTrue(
            self.order_model._job_import_order(self.instance.id, changed_payload)
        )
        replacement = sale.partner_shipping_id
        self.assertNotEqual(replacement, first_contact)
        self.assertEqual(replacement.phone, updated["phone"])
        self.assertEqual(first_contact.phone, original["phone"])
        child_count = len(sale.partner_id.child_ids)
        self.assertTrue(
            self.order_model._job_import_order(self.instance.id, changed_payload)
        )
        self.assertEqual(len(sale.partner_id.child_ids), child_count)
        self.assertEqual(sale.partner_shipping_id, replacement)

    # Restored OCA regression coverage for discount, refunds and delivery.
    def test_fixed_discount_over_several_units_matches_shopify_total(self):
        payload = self._payload(order_id=30, quantity=3, total="20.00")
        line = payload["lineItems"]["nodes"][0]
        line["discountedUnitPriceAfterAllDiscountsSet"] = self._bag("6.67")
        line["discountAllocations"] = [{"allocatedAmountSet": self._bag("10.00")}]

        binding = self.order_model.browse(
            self.order_model._job_import_order(self.instance.id, payload)
        )

        self.assertEqual(binding.state, "synced")
        self.assertEqual(binding.odoo_id.amount_total, 20)

    def _refunded_before_invoice_payload(
        self, order_id, refunded_quantity, refunded_shipping=None
    ):
        """Three units at 10.00, some of them refunded before any invoice.

        With ``refunded_shipping``, the order also has 5.00 of shipping and
        the refund returns that much of it.
        """
        remaining = 3 - refunded_quantity
        shipping = 5 if refunded_shipping is not None else 0
        current_shipping = shipping - (refunded_shipping or 0)
        total = f"{remaining * 10 + current_shipping}.00"
        payload = self._payload(
            order_id=order_id,
            quantity=3,
            total=total,
            displayFinancialStatus="PARTIALLY_REFUNDED",
            currentShippingPriceSet=self._bag(f"{current_shipping}.00"),
        )
        line = payload["lineItems"]["nodes"][0]
        line["currentQuantity"] = remaining
        line["refundableQuantity"] = remaining
        refund_shipping_lines = []
        if refunded_shipping is not None:
            payload["shippingLines"] = {
                "nodes": [
                    {
                        "id": f"gid://shopify/ShippingLine/{order_id}3",
                        "title": "Standard",
                        "code": "standard",
                        "originalPriceSet": self._bag(f"{shipping}.00"),
                        # Shopify: "The current shipping price after applying
                        # refunds, after applying discounts."
                        "currentDiscountedPriceSet": self._bag(
                            f"{current_shipping}.00"
                        ),
                        "taxLines": [],
                    }
                ]
            }
            refund_shipping_lines = [
                {
                    "id": f"gid://shopify/RefundShippingLine/{order_id}4",
                    "subtotalAmountSet": self._bag(f"{refunded_shipping}.00"),
                }
            ]
        refunded_total = refunded_quantity * 10 + (refunded_shipping or 0)
        payload["refunds"] = {
            "nodes": [
                {
                    "id": f"gid://shopify/Refund/{order_id}1",
                    "createdAt": "2026-01-11T10:00:00Z",
                    "totalRefundedSet": self._bag(f"{refunded_total}.00"),
                    "refundLineItems": {
                        "nodes": [
                            {
                                "id": f"gid://shopify/RefundLineItem/{order_id}2",
                                "lineItem": {"id": "gid://shopify/LineItem/30"},
                                "quantity": refunded_quantity,
                                "restockType": "NO_RESTOCK",
                                "subtotalSet": self._bag(
                                    f"{refunded_quantity * 10}.00"
                                ),
                                "totalTaxSet": self._bag("0.00"),
                            }
                        ]
                    },
                    "refundShippingLines": {"nodes": refund_shipping_lines},
                    "orderAdjustments": {"nodes": []},
                    "transactions": {"nodes": []},
                }
            ]
        }
        return payload

    def test_refund_before_invoice_keeps_the_current_quantity(self):
        payload = self._refunded_before_invoice_payload(94, refunded_quantity=1)

        binding_id = self.order_model._job_import_order(self.instance.id, payload)

        binding = self.order_model.browse(binding_id)
        self.assertEqual(binding.state, "synced", binding.error_message)
        self.assertEqual(binding.odoo_id.order_line.product_uom_qty, 2)
        self.assertEqual(binding.odoo_id.amount_total, 20)

    def test_partial_refund_before_invoice_does_not_cancel_the_order(self):
        self.instance.refund_uninvoiced_policy = "cancel"
        payload = self._refunded_before_invoice_payload(95, refunded_quantity=2)

        binding_id = self.order_model._job_import_order(self.instance.id, payload)

        sale_order = self.order_model.browse(binding_id).odoo_id
        self.assertNotEqual(sale_order.state, "cancel")
        self.assertEqual(sale_order.order_line.product_uom_qty, 1)

    def test_full_refund_before_invoice_cancels_the_order(self):
        self.instance.refund_uninvoiced_policy = "cancel"
        payload = self._refunded_before_invoice_payload(96, refunded_quantity=3)

        binding_id = self.order_model._job_import_order(self.instance.id, payload)

        self.assertEqual(self.order_model.browse(binding_id).odoo_id.state, "cancel")

    def test_full_refund_with_shipping_before_invoice_cancels_the_order(self):
        self.instance.refund_uninvoiced_policy = "cancel"
        payload = self._refunded_before_invoice_payload(
            97, refunded_quantity=3, refunded_shipping=5
        )

        binding_id = self.order_model._job_import_order(self.instance.id, payload)

        binding = self.order_model.browse(binding_id)
        self.assertEqual(binding.odoo_id.state, "cancel", binding.error_message)

    def _delivered(self):
        return patch.object(type(self.order_model), "_is_delivered", return_value=True)

    def test_refund_after_delivery_marked_handled_is_not_applied_again(self):
        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id, self._payload(order_id=98, quantity=3, total="30.00")
            )
        )
        sale_order = binding.odoo_id
        refunded = self._refunded_before_invoice_payload(98, refunded_quantity=1)
        refunded["updatedAt"] = "2026-01-11T10:00:00Z"

        with self._delivered():
            with self.assertRaises(UserError):
                binding.action_mark_handled()
            self.assertFalse(
                self.order_model._job_import_order(self.instance.id, refunded)
            )
            self.assertIn("manual resolution", binding.error_message)

            with trap_jobs() as trap:
                binding.action_mark_handled()
            trap.assert_jobs_count(1)
            self.assertEqual(
                binding.handled_state,
                {
                    "lines": [{"id": "gid://shopify/LineItem/30", "quantity": 2}],
                    "cancelled": False,
                },
            )
            self.assertEqual(binding.handled_uid, self.env.user)
            self.assertEqual(binding.refund_binding_ids.state, "synced")
            # The handled refund shows its amount, as an imported one does.
            self.assertEqual(binding.refund_binding_ids.amount, "10.00")
            self.assertEqual(binding.refund_binding_ids.currency_id, self.currency)

            result = self.order_model._job_import_order(self.instance.id, refunded)
            self.assertTrue(result, binding.error_message)
            self.assertEqual(binding.state, "synced")
            # The Odoo lines stay as the person left them.
            self.assertEqual(sale_order.order_line.product_uom_qty, 3)
            # The line binding shows Shopify's current quantity.
            self.assertEqual(binding.line_binding_ids.current_quantity, 2)
            self.assertEqual(len(binding.refund_binding_ids), 1)

            refunded["tags"] = ["web", "vip"]
            refunded["updatedAt"] = "2026-01-12T10:00:00Z"
            result = self.order_model._job_import_order(self.instance.id, refunded)
            self.assertTrue(result, binding.error_message)
            self.assertEqual(sale_order.shopify_tags, "web, vip")

            # A new edit of the lines needs manual resolution again.
            edited = self._refunded_before_invoice_payload(98, refunded_quantity=2)
            edited["updatedAt"] = "2026-01-13T10:00:00Z"
            self.assertFalse(
                self.order_model._job_import_order(self.instance.id, edited)
            )
            self.assertIn("manual resolution", binding.error_message)
            self.assertEqual(sale_order.order_line.product_uom_qty, 3)

    def test_cancel_after_delivery_marked_handled_is_imported(self):
        binding = self.order_model.browse(
            self.order_model._job_import_order(
                self.instance.id, self._payload(order_id=99)
            )
        )
        sale_order = binding.odoo_id

        with self._delivered():
            self.assertFalse(
                self.order_model._job_import_order(
                    self.instance.id, self._cancelled_payload(order_id=99)
                )
            )
            self.assertIn("already delivered", binding.error_message)
            with trap_jobs():
                binding.action_mark_handled()
            self.assertTrue(binding.handled_state["cancelled"])

            result = self.order_model._job_import_order(
                self.instance.id, self._cancelled_payload(order_id=99)
            )

        self.assertTrue(result, binding.error_message)
        self.assertEqual(binding.state, "synced")
        self.assertNotEqual(sale_order.state, "cancel")
        self.assertEqual(sale_order.order_line.product_uom_qty, 1)

    def test_bfcm_multi_line_tax_included_discount_rounding_1351(self):
        # Anonymized monetary fixture from Shopify #1351 (2026-11-22).
        # Rounded discounted unit prices drift by EUR 0.02 for two 2-unit
        # lines; tax-included global rounding must still match the order.
        self.instance.company_id.tax_calculation_rounding_method = "round_globally"
        eur = (
            self.env["res.currency"]
            .with_context(active_test=False)
            .search([("name", "=", "EUR")], limit=1)
        )
        self.assertTrue(eur)
        self.currency = eur

        tax = self.env["account.tax"].create(
            {
                "name": "BF-CM German VAT 19% included",
                "amount": 19,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": self.instance.company_id.id,
                "price_include_override": "tax_included",
            }
        )
        self.env["shopify.tax.mapping"].create(
            {
                "instance_id": self.instance.id,
                "rate": "0.19",
                "country_id": self.env.ref("base.de").id,
                "price_included": True,
                "tax_id": tax.id,
            }
        )

        # quantity, original unit gross, discount allocation, rounded unit
        # after all discounts, Shopify included VAT for each full line.
        items = (
            (1, "1.99", "0.14", "1.85", "0.30"),
            (1, "51.56", "3.61", "47.95", "7.66"),
            (2, "11.90", "1.67", "11.07", "3.53"),
            (2, "17.85", "2.50", "16.60", "5.30"),
            (1, "13.09", "0.92", "12.17", "1.94"),
            (1, "17.85", "1.25", "16.60", "2.65"),
            (1, "17.85", "1.25", "16.60", "2.65"),
            (2, "11.90", "1.67", "11.07", "3.53"),
            (1, "14.28", "1.00", "13.28", "2.12"),
            (1, "23.80", "1.66", "22.14", "3.53"),
            (1, "11.90", "0.83", "11.07", "1.77"),
            (1, "19.84", "1.38", "18.46", "2.95"),
            (1, "2.48", "0.17", "2.31", "0.37"),
        )
        payload = self._payload(order_id=1351, total="250.64")
        payload.update(
            {
                "currencyCode": "EUR",
                "presentmentCurrencyCode": "EUR",
                "taxesIncluded": True,
                "shippingAddress": {"countryCodeV2": "DE"},
                "currentSubtotalPriceSet": self._bag("239.89"),
                "currentTotalDiscountsSet": self._bag("18.05"),
                "currentShippingPriceSet": self._bag("10.75"),
                "currentTotalTaxSet": self._bag("40.02"),
                "currentTotalPriceSet": self._bag("250.64"),
            }
        )
        lines = []
        for number, (quantity, unit, discount, rounded, included_tax) in enumerate(
            items, start=1
        ):
            lines.append(
                {
                    "id": f"gid://shopify/LineItem/135100{number}",
                    "name": f"Anonymized product {number}",
                    "sku": f"BF-CM-{number}",
                    "quantity": quantity,
                    "currentQuantity": quantity,
                    "refundableQuantity": quantity,
                    "variant": {"id": self.variant_binding.shopify_id},
                    "product": {"id": self.template_binding.shopify_id},
                    "originalUnitPriceSet": self._bag(unit),
                    "discountedUnitPriceAfterAllDiscountsSet": self._bag(rounded),
                    "discountAllocations": [
                        {"allocatedAmountSet": self._bag(discount)}
                    ],
                    "taxLines": [
                        {
                            "title": "DE MwSt",
                            "rate": "0.19",
                            "priceSet": self._bag(included_tax),
                        }
                    ],
                }
            )
        payload["lineItems"] = {"nodes": lines}
        payload["shippingLines"] = {
            "nodes": [
                {
                    "id": "gid://shopify/ShippingLine/1351",
                    "title": "Shipping",
                    "originalPriceSet": self._bag("10.75"),
                    "currentDiscountedPriceSet": self._bag("10.75"),
                    "taxLines": [
                        {
                            "title": "DE MwSt",
                            "rate": "0.19",
                            "priceSet": self._bag("1.72"),
                        }
                    ],
                }
            ]
        }

        normalized = normalize_order_payload(payload)
        subtotal = sum(
            (
                Decimal(line["discounted_total"]["shop"]["amount"])
                for line in normalized["lines"]
            ),
            Decimal("0"),
        )
        self.assertEqual(subtotal, Decimal("239.89"))
        self.assertEqual(
            sum(
                (
                    Decimal(item["discount_allocations"][0]["amount"]["shop"]["amount"])
                    for item in normalized["lines"]
                ),
                Decimal("0"),
            ),
            Decimal("18.05"),
        )
        result = self.order_model._job_import_order(self.instance.id, payload)
        binding = self.order_model.search(
            [
                ("instance_id", "=", self.instance.id),
                ("shopify_id", "=", "gid://shopify/Order/1351"),
            ],
            limit=1,
        )
        self.assertTrue(result, binding.error_message)
        self.assertEqual(binding.state, "synced")
        product_lines = binding.line_binding_ids.filtered(
            lambda item: not item.is_shipping
        )
        self.assertEqual(len(product_lines), 13)
        self.assertAlmostEqual(binding.odoo_id.amount_tax, 40.02, places=2)
        self.assertAlmostEqual(binding.odoo_id.amount_total, 250.64, places=2)

    # Regression: Shopify #1352 (anonymised). Fulfillment was marked done,
    # but Shopify subsequently closed a return and restocked all six units.
    # These expectations intentionally fail against the deployed connector.
    def _delivered_restock_fixture(self, order_id, quantities=(4, 1, 1)):
        self.instance.write(
            {
                "order_confirmation_policy": "auto",
                "order_invoicing_policy": "manual",
                "fulfillment_export_enabled": False,
            }
        )
        warehouse = self.env["stock.warehouse"].search(
            [("company_id", "=", self.instance.company_id.id)], limit=1
        )
        self.assertTrue(warehouse)
        total_quantity = sum(quantities)
        self.env["stock.quant"]._update_available_quantity(
            self.goods, warehouse.lot_stock_id, total_quantity
        )
        payload = self._payload(
            order_id=order_id,
            quantity=quantities[0],
            total=f"{10 * total_quantity}.00",
            displayFinancialStatus="PAID",
        )
        lines = []
        for index, quantity in enumerate(quantities, start=1):
            line = dict(payload["lineItems"]["nodes"][0])
            line.update(
                {
                    "id": f"gid://shopify/LineItem/{order_id}0{index}",
                    "name": f"Anonymous stock item {index}",
                    "sku": f"REGRESSION-{order_id}-{index}",
                    "quantity": quantity,
                    "currentQuantity": quantity,
                    "refundableQuantity": quantity,
                    "variant": {"id": self.goods_variant_binding.shopify_id},
                    "product": {
                        "id": self.goods_variant_binding.template_binding_id.shopify_id
                    },
                }
            )
            lines.append(line)
        payload["lineItems"] = {"nodes": lines}
        binding_id = self.order_model._job_import_order(self.instance.id, payload)
        binding = self.order_model.browse(binding_id)
        self.assertEqual(binding.state, "synced", binding.error_message)
        self.assertEqual(binding.odoo_id.state, "sale")
        self.assertFalse(binding.odoo_id.invoice_ids)
        picking = binding.odoo_id.picking_ids.filtered(
            lambda item: item.picking_type_id.code == "outgoing"
        )
        self.assertEqual(len(picking), 1)
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.button_validate()
        self.assertEqual(picking.state, "done")
        self.assertEqual(sum(picking.move_ids.mapped("quantity")), total_quantity)
        return binding, picking, payload

    def _delivered_restock_update(
        self, payload, *, restock_type="RETURN", restocked=True
    ):
        from copy import deepcopy

        update = deepcopy(payload)
        update["updatedAt"] = "2026-01-13T10:00:00Z"
        update["displayFulfillmentStatus"] = "FULFILLED"
        update["currentSubtotalPriceSet"] = self._bag("0.00")
        update["currentTotalPriceSet"] = self._bag("0.00")
        for line in update["lineItems"]["nodes"]:
            line["currentQuantity"] = 0
            line["refundableQuantity"] = 0
        refund_lines = []
        return_lines = []
        for index, line in enumerate(update["lineItems"]["nodes"], start=1):
            quantity = line["quantity"]
            refund_lines.append(
                {
                    "id": f"gid://shopify/RefundLineItem/{update['name'][1:]}0{index}",
                    "lineItem": {"id": line["id"]},
                    "quantity": quantity,
                    "restockType": restock_type,
                    "restocked": restocked,
                    "subtotalSet": self._bag(f"{10 * quantity}.00"),
                    "totalTaxSet": self._bag("0.00"),
                }
            )
            return_lines.append(
                {
                    "id": f"gid://shopify/ReturnLineItem/{update['name'][1:]}0{index}",
                    "quantity": quantity,
                    "refundableQuantity": 0,
                    "refundedQuantity": quantity,
                    "fulfillmentLineItem": {"lineItem": {"id": line["id"]}},
                }
            )
        update["refunds"] = {
            "nodes": [
                {
                    "id": f"gid://shopify/Refund/{update['name'][1:]}",
                    "createdAt": "2026-01-13T09:59:00Z",
                    "totalRefundedSet": self._bag("0.00"),
                    "refundLineItems": {"nodes": refund_lines},
                    "refundShippingLines": {"nodes": []},
                    "orderAdjustments": {"nodes": []},
                    "transactions": {
                        "nodes": [
                            {
                                "id": f"gid://shopify/OrderTransaction/{update['name'][1:]}",
                                "gateway": "shopify_payments",
                                "kind": "REFUND",
                                "status": "PENDING",
                                "amountSet": self._bag(
                                    f"{10 * sum(x['quantity'] for x in refund_lines)}.00"
                                ),
                            }
                        ]
                    },
                }
            ]
        }
        update["returns"] = {
            "nodes": [
                {
                    "id": f"gid://shopify/Return/{update['name'][1:]}",
                    "name": f"RETURN-{update['name'][1:]}",
                    "status": "CLOSED",
                    "createdAt": "2026-01-13T09:50:00Z",
                    "closedAt": "2026-01-13T10:00:00Z",
                    "totalQuantity": sum(line["quantity"] for line in refund_lines),
                    "returnLineItems": {"nodes": return_lines},
                    "exchangeLineItems": {"nodes": []},
                }
            ]
        }
        return update

    def _linked_stock_returns(self, picking):
        return self.env["stock.move"].search(
            [
                ("origin_returned_move_id", "in", picking.move_ids.ids),
            ]
        )

    def test_delivered_shopify_restock_imports_and_reverses_stock_once(self):
        """#1352: restocked RETURN, closed return, pending monetary refund."""
        binding, picking, original = self._delivered_restock_fixture(1352001)
        updated = self._delivered_restock_update(original)
        result = self.order_model._job_import_order(self.instance.id, updated)

        self.assertEqual(result, binding.id, binding.error_message)
        self.assertEqual(binding.state, "synced", binding.error_message)
        self.assertEqual(binding.return_binding_ids.mapped("return_status"), ["CLOSED"])
        self.assertEqual(len(binding.refund_binding_ids), 1)
        self.assertEqual(binding.refund_binding_ids.state, "synced")
        self.assertFalse(binding.odoo_id.invoice_ids)
        self.assertEqual(picking.state, "done")
        returned_moves = self._linked_stock_returns(picking)
        self.assertEqual(sum(returned_moves.mapped("quantity")), 6)
        self.assertTrue(all(move.state == "done" for move in returned_moves))

        # A newer webhook containing the same Shopify refund must be harmless.
        updated["updatedAt"] = "2026-01-14T10:00:00Z"
        result = self.order_model._job_import_order(self.instance.id, updated)
        self.assertEqual(result, binding.id, binding.error_message)
        self.assertEqual(len(binding.refund_binding_ids), 1)
        self.assertEqual(sum(self._linked_stock_returns(picking).mapped("quantity")), 6)

    def test_delivered_refund_without_restock_does_not_increase_stock(self):
        binding, picking, original = self._delivered_restock_fixture(
            1352002, quantities=(1,)
        )
        updated = self._delivered_restock_update(
            original, restock_type="NO_RESTOCK", restocked=False
        )
        result = self.order_model._job_import_order(self.instance.id, updated)

        self.assertEqual(result, binding.id, binding.error_message)
        self.assertEqual(binding.state, "synced", binding.error_message)
        self.assertEqual(len(binding.refund_binding_ids), 1)
        self.assertFalse(binding.refund_binding_ids.restock_requested)
        self.assertFalse(self._linked_stock_returns(picking))
        self.assertEqual(picking.state, "done")

    def test_pending_refund_does_not_block_confirmed_shopify_restock(self):
        """A pending payment transaction is not a pending stock receipt."""
        binding, picking, original = self._delivered_restock_fixture(
            1352003, quantities=(1,)
        )
        updated = self._delivered_restock_update(original)
        transactions = updated["refunds"]["nodes"][0]["transactions"]["nodes"]
        self.assertEqual(transactions[0]["status"], "PENDING")
        self.assertEqual(
            updated["refunds"]["nodes"][0]["totalRefundedSet"]["shopMoney"]["amount"],
            "0.00",
        )
        result = self.order_model._job_import_order(self.instance.id, updated)

        self.assertEqual(result, binding.id, binding.error_message)
        returned = self._linked_stock_returns(picking)
        self.assertEqual(sum(returned.mapped("quantity")), 1)
        self.assertTrue(all(move.state == "done" for move in returned))
        self.assertFalse(binding.odoo_id.invoice_ids)
