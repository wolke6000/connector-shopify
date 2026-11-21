# OCA 19.0 connector upgrade — isolated database validation

**Release gate: OPEN (production deployment).** On 2026-11-21 an isolated,
network-restricted post-cleanup database clone was upgraded using the PR source; image
bindings and variant relations were preserved. No production database upgrade or restore
was performed. Fresh-install CI is not migration evidence.

## Scope

Upgrade the _existing_ Switchology connector schema from the deployed fork to the
OCA-integrated source (`shopify_connector` `19.0.1.7.0`) and its new
`shopify_connector_website_sale` addon, with a post-cleanup database and matching
filestore. Demonstrate that bindings, module ownership, and safe configuration survive
the migration. This is **not** an instruction to deploy.

Relevant upstream migration: `shopify_connector/migrations/19.0.1.1.0/pre-migration.py`.

The upstream migration:

1. Requires an `ir_module_module` row for `shopify_connector_website_sale` when legacy
   image bindings exist; otherwise it aborts.
2. Reassigns image model/field XML IDs, selected constraints, and the image/variant
   relation to the glue addon (leaving binding-mixin constraints).
3. Marks the glue module `to install` in the **same upgrade**.

The main connector no longer depends on `website_sale`; the glue module depends on both
`shopify_connector` and `website_sale` and is auto-installable. The module upgrade
therefore requires a dedicated migration test rather than relying only on a successful
new install.

## Environment and preconditions

- Use an **isolated** clone of the **post-2026-11-19-cleanup** production PostgreSQL
  database **and its matching filestore**.
- Before starting Odoo, enforce network-layer **outbound egress denial** (Shopify, SMTP,
  delivery services, payment APIs, webhooks). No real credentials, writable secrets,
  live URLs or production queue backend may be available to the clone. Mask any personal
  data in exported evidence.
- Disable all cron workers, background queue runners and queued job processing;
  `--max-cron-threads=0` alone does **not** prove isolation.
- Do not put the clone behind any public web endpoint. Install both addons from the
  exact PR branch, with the identical Odoo 19 dependencies as production.
- Record original production version, runtime connector gitlink, database
  backup/checkpoint, filestore fingerprint and the clone's source commit.
- Ensure `shopify_connector_website_sale` is discoverable in Odoo's module registry
  **before** running the core-module upgrade. Run an Apps/module-list refresh on the
  isolated clone if needed; if existing image bindings are present and the glue row is
  absent, **stop**.

## Read-only baseline (against the CLONE, before upgrade)

Run the following queries only in the isolated clone; capture results in a
restricted-access evidence log. The conditional table checks are important when
validating a fresh install vs. a migrated database.

```sql
-- Modules must be discoverable; the original version and state matter.
SELECT name, state, latest_version
FROM ir_module_module
WHERE name IN (
  'shopify_connector', 'shopify_connector_website_sale', 'website_sale',
  'queue_job'
)
ORDER BY name;

SELECT to_regclass('public.shopify_product_image') AS image_binding_table;
SELECT to_regclass('public.shopify_product_image_variant_rel') AS image_variant_rel;
SELECT to_regclass('public.shopify_product_template') AS product_binding_table;

-- Only run this second query if the image-binding table exists:
SELECT instance_id, count(*) AS bindings,
       count(DISTINCT shopify_id) AS distinct_media_ids,
       count(DISTINCT odoo_id) AS distinct_odoo_images
FROM shopify_product_image
GROUP BY instance_id ORDER BY instance_id;

SELECT module, count(*) AS metadata_items
FROM ir_model_data
WHERE name LIKE '%shopify_product_image%'
   OR name LIKE 'field_product_image__%'
   OR name IN (
     'model_product_image',
     'field_shopify_product_template__image_binding_ids'
   )
GROUP BY module ORDER BY module;

SELECT name, module
FROM ir_model_constraint
WHERE model = (SELECT id FROM ir_model WHERE model = 'shopify.product.image')
ORDER BY name;

SELECT id, active, fulfillment_export_enabled,
       fulfillment_auto_validate_webhook, fulfillment_validate_unreserved,
       product_export_enabled
FROM shopify_instance ORDER BY id;
```

Also capture **aggregate** counts of `shopify.product.template` and
`shopify.product.variant` bindings, image/variant association rows, linked
`product.image` records, `sale.order`/Shopify-order bindings, and the historical-import
cutoff on each instance. Keep original primary-key mappings in **private**, controlled
test evidence (not in GitHub).

Do not run queries against a schema where fields do not yet exist; inspect the actual
database columns first. A missing newly introduced instance field is an upgrade
prerequisite to record, not a reason to alter production manually.

## Upgrade execution (CLONE ONLY)

1. Take an additional immutable restore point of the isolated clone.
2. Confirm egress is blocked and no queue workers/crons are running.
3. Refresh the module list until the glue module is discoverable. Re-run the module-name
   baseline query. Do **not** manually change module states by SQL as a shortcut.
4. With both addon directories present, execute the controlled Odoo 19 upgrade of
   `shopify_connector`, e.g. using the isolated service's standard config with
   `--stop-after-init --no-http --max-cron-threads=0 -u shopify_connector`. Use the
   **clone** database name; do not run `-u all`.
5. The migration should select `shopify_connector_website_sale` for installation. If it
   fails, preserve logs and **discard the clone** before attempting another variant; do
   not hand-edit production metadata.
6. Confirm `shopify_connector_website_sale` is installed and its Odoo model registry
   provides `shopify.product.image` and its image/variant relations.

## Post-upgrade invariants

- The core module is at the intended version, the glue module is installed, the Odoo
  registry initializes, and **no** image-binding table, associated relation or linked
  image data disappeared.
- Compare image-binding counts **per instance**, existing image binding IDs,
  `shopify_id`, `template_binding_id`, `variant_binding_id`, `variant_binding_ids`,
  `odoo_id`, and all existing relationships to their baseline snapshots. Null or
  orphaned links are a **FAIL**, not silently reconstructed data.
- Confirm the expected `ir_model_data`, `ir_model_constraint`, and `ir_model_relation`
  module ownership, including retained mixin constraints.
- Rerun the instance export/validation flags. For Switchology, outbound product and
  fulfillment exports must remain disabled; imported Shopify delivery auto-validation
  and reserved-only policy remain **independently configurable**. Newly introduced
  Boolean defaults must not change existing instance choices.
- Preserve order bindings, protected historical draft order(s), `order_drift_pending`,
  and both import and confirmation cutoffs. Check there was no reimport of the 270
  intentionally removed historical orders or their 281 bindings.
- Check all active/pending queue jobs, webhook events and scheduled actions. The
  migration must not enqueue or execute Shopify mutations, cancel/reserve physical
  stock, post invoices, or send mail.
- Execute read-only Odoo ORM smoke checks of product and image bindings, plus
  migration-specific tests **with mocked external APIs**, still without egress.
- Preserve sanitized logs, source SHA, before/after counts and an explicit PASS/FAIL per
  invariant. A green fresh-install CI run does not satisfy this gate.

## Failure and release decision

Any missing image binding, incorrect module ownership, unexpectedly enabled write flag,
historical order recreation, or external API operation is a **STOP**. Analyze on a fresh
clone, amend the PR and repeat the upgrade test.

A production upgrade requires a _separate_ approved release plan with an on-demand
backup, rehearsed restore, explicit downtime/queue policy, and independent review. **Do
not change the Odoo submodule gitlink or production services as part of this
validation.**

## Isolated validation evidence — 2026-11-21

- Tested an isolated PostgreSQL clone, with an internal-only Docker network, read-only
  source filestore, no external Shopify access, and disabled queue/crons.
- `shopify_connector` upgrade from `19.0.1.0.3` to `19.0.1.7.0` succeeded, including the
  `shopify_connector_website_sale` addon.
- Verified preservation of 127 image bindings and 38 image/variant relationships; module
  registry, constraints, and ORM binding checks passed.
- A separate test-database clone (`switchology_pr17_tests`) passed 21 selected Odoo
  19.0-20260908 regressions: five order/cancellation cases, six outbound fulfillment
  guards, five inbound fulfillment cases, and five refunds.
- Product import/reimport idempotency, image pruning, product export guards, and
  historical order cutoff passed an isolated mocked/rollback smoke test.
- A read-only snapshot inventory inspection found instance 1 with inventory import,
  product export, customer export, and fulfillment export disabled; 22 of 54 product
  bindings and 36 of 83 variant bindings opted into inventory sync. All 36 were tracked;
  no tested cross-instance or parent mismatch; one active Shopify location mapped to
  internal `WH/Stock`.
- These are **snapshot** observations, not verification of live production configuration
  or a full independent security review.
- Current PR-head CI, outbound mutation audit, release rollback plan, and production
  deployment approval are still separate gates.

## Failed first rehearsal and corrected source snapshot

The first isolated migration rehearsal used the older CP2A clone, with 76 `sale.order`
records, 82 Shopify order bindings, 143 image bindings and 42 image/variant
relationships. Seven Shopify media IDs were each bound to more than one product (20
bindings, 13 additional occurrences). The old constraint allowed
`UNIQUE(instance_id, template_binding_id, shopify_id)`. During the upgrade, the new OCA
`UNIQUE(instance_id, shopify_id)` index **could not be created**. Odoo nevertheless
exited with code 0 and reported installed module versions. **This rehearsal failed its
schema gate** despite its successful process exit code; it is not evidence of a valid
migration.

A Shopify Admin API investigation in the earlier review established that the shared
media IDs existed across five distinct Shopify products, including the 3x5 bundle and
its components/Beta products. They were not necessarily corrupt database rows. The
merchant subsequently adjusted Shopify product media assignments (no connector source
change). A newer production-derived snapshot, identified as `39689e91`, contained **79**
sale orders, **85** Shopify order bindings, **127** image bindings, **38** image/variant
relations, and **zero** repeated `(instance_id, shopify_id)` media keys before upgrade.
This second snapshot was separately restored with its filestore and was the source of
the successful isolated migration reported above.

Post-upgrade evidence for `39689e91` included the expected unique constraint, zero
invalid image relationships, preservation of all 127/38 records, correct
`shopify_connector_website_sale` XML-ID ownership, preserved manual invoicing and
order/confirmation cutoffs (`2026-11-07`), and zero pending/running queue jobs (16,504
done; 39 cancelled). Inbound webhook auto-validation was enabled, independently of
disabled outbound fulfillment export. This is intentional, not a new outbound export
authorization.

**Release implication:** A zero-exit Odoo upgrade does not establish successful index
creation. A fresh production preflight must check for repeated media keys, and
post-upgrade checks must inspect PostgreSQL constraints/indexes and relevant Odoo logs.
The clean Shopify state observed in November is not a permanent guarantee; future shared
media IDs could recreate the condition. Do not delete valid cross-product media links or
weaken upstream uniqueness without a separately reviewed decision. Recheck before
deployment.
