from datetime import datetime
from unittest.mock import Mock, patch

from freezegun import freeze_time

from odoo import SUPERUSER_ID

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.queue_job.tests.common import trap_jobs
from odoo.addons.shopify_connector.lib.client import ShopifyError


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
        self.assertEqual(binding.odoo_id.order_line.filtered(
            lambda line: not line.shopify_is_shipping
        ).product_uom_qty, 1)

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
                self._payload(
                    order_id=4,
                    cancelledAt="2026-01-12T10:00:00Z",
                    updatedAt="2026-01-12T10:00:00Z",
                ),
            )

        self.assertFalse(result)
        self.assertNotEqual(binding.odoo_id.state, "cancel")

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

    def test_high_risk_sets_hold_and_release_action(self):
        payload = self._payload(order_id=6)
        payload["risk"] = {"assessments": [{"riskLevel": "HIGH"}]}

        binding_id = self.order_model._job_import_order(self.instance.id, payload)
        order = self.order_model.browse(binding_id).odoo_id

        self.assertTrue(order.shopify_risk_hold)
        order.action_release_shopify_risk_hold()
        self.assertFalse(order.shopify_risk_hold)

    def test_completed_draft_links_real_order_without_duplicate_sale(self):
        draft = {
            "id": "gid://shopify/DraftOrder/90",
            "name": "#D90",
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
            "order": {"id": "gid://shopify/Order/90"},
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
