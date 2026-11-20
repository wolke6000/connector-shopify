import base64
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.shopify_connector.tests.common import (
    MAIN_IMAGE_ID,
    SHARED_IMAGE_ID,
    VARIANT_IMAGE_ID,
    download_test_image,
    image_product_payload,
    png_bytes,
)


class TestShopifyExtraProductMedia(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.instance = cls.env["shopify.instance"].create(
            {
                "name": "Media Shop",
                "shop_url": "media-shop.myshopify.com",
                "access_token": "test-token",
                "webhook_secret": "test-secret",
            }
        )
        cls.instance.state = "connected"
        cls.binding_model = cls.env["shopify.product.template"]

    def _import(self):
        with patch.object(
            type(self.binding_model),
            "_download_image",
            side_effect=download_test_image,
        ):
            return self.binding_model.browse(
                self.binding_model._job_import_product(
                    self.instance.id, image_product_payload(), payload_normalized=True
                )
            )

    def test_extra_media_only_keep_images_without_main_or_variant_role(self):
        binding = self._import()

        self.assertEqual(binding.state, "synced")
        self.assertEqual(
            binding.image_binding_ids.mapped("shopify_id"), [SHARED_IMAGE_ID]
        )
        self.assertEqual(
            binding.image_binding_ids.odoo_id.product_tmpl_id, binding.odoo_id
        )
        self.assertEqual(
            binding.image_binding_ids.variant_binding_ids,
            binding.variant_binding_ids,
        )
        self.assertEqual(
            self.binding_model._tracked_image_ids(binding),
            {MAIN_IMAGE_ID, VARIANT_IMAGE_ID, SHARED_IMAGE_ID},
        )
        self.assertFalse(self.binding_model._unmanaged_shopify_image_ids(binding, []))

    def test_reimport_removes_main_image_from_extra_media(self):
        binding = self._import()
        extra_main = self.env["product.image"].create(
            {
                "name": "Main image as extra media",
                "image_1920": base64.b64encode(png_bytes("red")),
                "product_tmpl_id": binding.odoo_id.id,
            }
        )
        self.env["shopify.product.image"].create(
            {
                "instance_id": self.instance.id,
                "shopify_id": MAIN_IMAGE_ID,
                "template_binding_id": binding.id,
                "odoo_id": extra_main.id,
            }
        )
        self.instance.field_mapping_ids.filtered(
            lambda mapping: mapping.field == "images"
        ).owner = "shopify"

        self._import()

        self.assertFalse(extra_main.exists())
        self.assertEqual(
            binding.image_binding_ids.mapped("shopify_id"), [SHARED_IMAGE_ID]
        )

    def test_shared_media_gid_is_scoped_per_product(self):
        product_a = self.env["product.product"].create(
            {
                "name": "Shared Media A",
                "company_id": self.instance.company_id.id,
            }
        )
        product_b = self.env["product.product"].create(
            {
                "name": "Shared Media B",
                "company_id": self.instance.company_id.id,
            }
        )
        binding_a = self.env["shopify.product.template"].create(
            {
                "instance_id": self.instance.id,
                "shopify_id": "gid://shopify/Product/601",
                "odoo_id": product_a.product_tmpl_id.id,
            }
        )
        binding_b = self.env["shopify.product.template"].create(
            {
                "instance_id": self.instance.id,
                "shopify_id": "gid://shopify/Product/602",
                "odoo_id": product_b.product_tmpl_id.id,
            }
        )
        image_payload = [
            {
                "id": "gid://shopify/MediaImage/603",
                "url": "https://cdn.shopify.com/shared.png",
                "alt": "Shared image",
            }
        ]
        image_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
        )
        sync_model = self.env["shopify.product.template"]

        with patch.object(
            type(sync_model),
            "_download_image",
            autospec=True,
            return_value=image_bytes,
        ):
            sync_model._sync_images(binding_a, image_payload, [], prune=True)
            sync_model._sync_images(binding_b, image_payload, [], prune=True)

        image_bindings = self.env["shopify.product.image"].search(
            [
                ("instance_id", "=", self.instance.id),
                ("shopify_id", "=", "gid://shopify/MediaImage/603"),
            ]
        )
        self.assertEqual(len(image_bindings), 2)
        self.assertEqual(
            set(image_bindings.mapped("template_binding_id").ids),
            {binding_a.id, binding_b.id},
        )
        self.assertEqual(len(image_bindings.mapped("odoo_id")), 2)
        self.assertEqual(
            set(image_bindings.mapped("odoo_id.product_tmpl_id").ids),
            {product_a.product_tmpl_id.id, product_b.product_tmpl_id.id},
        )

    def test_image_change_respects_disabled_product_export(self):
        # SW-OCA-004/010: image triggers are owned by the website_sale glue.
        product = self.env["product.product"].create(
            {"name": "Bound no media export", "company_id": self.instance.company_id.id}
        )
        self.env["shopify.product.template"].create(
            {
                "instance_id": self.instance.id,
                "shopify_id": "gid://shopify/Product/9801",
                "odoo_id": product.product_tmpl_id.id,
                "status": "ACTIVE",
                "state": "synced",
            }
        )
        self.instance.product_export_enabled = False
        export_model = self.env["shopify.product.template"]
        export_class = type(export_model)
        original = export_class._enqueue_product_export
        reached = []

        def trace_enqueue(recordset, instance, template):
            reached.append((instance.id, template.id))
            return original(recordset, instance, template)

        with (
            patch.object(export_class, "_enqueue_product_export", trace_enqueue),
            patch.object(export_class, "with_delay") as delayed,
        ):
            self.env["product.image"].create(
                {"name": "No media upload", "product_tmpl_id": product.product_tmpl_id.id}
            )
        self.assertEqual(
            reached, [(self.instance.id, product.product_tmpl_id.id)]
        )
        delayed.assert_not_called()

