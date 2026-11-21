# OCA 19.0 connector upgrade — isolated database validation

**Database migration gate: PASSED for the cleaned `39689e91` snapshot with the
production Odoo version `19.0-20261102` (2026-11-21). Production release gate: OPEN.**
The isolated upgrade preserved the tested records, relations and configuration. No
production database upgrade or restore was performed. CI and an Odoo exit code alone are
not database migration evidence.

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

## Production-version migration rehearsal — 2026-11-21 (PASS)

This is a **new, independent upgrade of the pre-migration database**, not an ORM check
of the already migrated September test database.

- **Source:** Restic snapshot `39689e91`, captured 2026-11-21 at 09:25 Europe/Vienna.
  Its pre-upgrade dump and matching filestore were copied and SHA256-verified locally; a
  separate immutable `pg_dump -Fc` checkpoint was retained after discovering the glue
  addon and before upgrading.
- **Reproducibility:** Production Odoo repository commit
  `7ce4eaaa57f2347e148ed3b20baa32494079b4b0`, installed connector gitlink
  `33a308f9774eba7fcc4bf3634397dbe873f0caec`, and tested PR source
  `3a49d51a09c8c123a8006a320c5faca27c6ae7db`. The four other OCA addon submodules
  matched their production pins. The new Odoo test image used the production-pinned
  multi-architecture Odoo base digest, its **AMD64** `19.0-20261102` variant, and
  `openupgradelib==3.13.7`; production itself runs ARM64. This matches the Odoo release
  and dependency version, **not the exact production ARM64 image binary**.
- **Isolation:** Local PostgreSQL 15, an internal-only Docker network (`Internal=true`),
  no public Odoo endpoint, no outbound Shopify access, `ODOO_QUEUE_JOB_CHANNELS=root:0`,
  `workers=0`, and `max_cron_threads=0`. No production or Shopify writes were performed.
- **Pre-upgrade clone:** `switchology_pr17_prodversion` had
  `shopify_connector=19.0.1.0.3`, 127 image bindings with 127 distinct Shopify media
  IDs, 38 image/variant relation rows, and 85 Shopify order bindings. After an Odoo
  module-list refresh, `shopify_connector_website_sale` was `uninstalled` and
  discoverable; its state was not edited manually.
- **Upgrade execution:** Isolated Odoo
  `--stop-after-init --no-http --workers=0 --max-cron-threads=0 -u shopify_connector`
  exited 0. The captured upgrade log had 54 lines and **zero** warning/error/traceback
  matches. Post-upgrade modules: `shopify_connector=19.0.1.7.0` and
  `shopify_connector_website_sale=19.0.1.0.0`, both installed.
- **Actual PostgreSQL schema:** The validated
  `shopify_product_image_instance_gid_unique` constraint and physical unique index both
  enforce `UNIQUE(instance_id, shopify_id)`. Zero duplicate media keys. The
  image/variant relation belongs to `shopify_connector_website_sale`; the two
  binding-mixin constraints remain owned by `shopify_connector`. Image-related XML-ID
  owner counts were 5 core, 28 glue, and 19 `website_sale` entries.
- **Relationship integrity:** All 127 image bindings and 38 image/variant relations
  remained. There were zero orphaned image, template, variant, or many-to-many links and
  zero duplicate many-to-many links. An isolated Odoo ORM registry load passed with 54
  Shopify product-template bindings, 83 variants, 85 order bindings, 79 `sale.order`
  records, 127 image bindings, and 38 ORM image/variant relations.
- **Exact before/after data comparison:** Restored the separate pre-upgrade checkpoint
  as `switchology_pr17_before_audit`. Deterministic SQL fingerprints of **all shared
  columns except `create_date` and `write_date`** matched for all 13 tables below.
  Fingerprints were ordered by stable primary/relationship keys and included complete
  row contents; no customer data or tokens were published.

| Table                               | Before |  After | Shared columns | New columns | Result |
| ----------------------------------- | -----: | -----: | -------------: | ----------: | ------ |
| `shopify_product_image`             |    127 |    127 |             14 |           0 | PASS   |
| `shopify_product_image_variant_rel` |     38 |     38 |              2 |           0 | PASS   |
| `shopify_product_template`          |     54 |     54 |             18 |           0 | PASS   |
| `shopify_product_variant`           |     83 |     83 |             20 |           0 | PASS   |
| `shopify_order`                     |     85 |     85 |             46 |           3 | PASS   |
| `sale_order`                        |     79 |     79 |             61 |           0 | PASS   |
| `sale_order_line`                   |    490 |    490 |             51 |           0 | PASS   |
| `shopify_instance`                  |      1 |      1 |             54 |           2 | PASS   |
| `shopify_field_mapping`             |      9 |      9 |              6 |           0 | PASS   |
| `queue_job`                         | 16,543 | 16,543 |             33 |           0 | PASS   |
| `stock_quant`                       |    561 |    561 |             18 |           0 | PASS   |
| `account_move`                      |      0 |      0 |             73 |           0 | PASS   |
| `res_partner`                       |  1,504 |  1,504 |             85 |           0 | PASS   |

**Result:** The **database migration gate is PASSED for this cleaned snapshot** and this
pinned source/Odoo combination. The 16,543 queue jobs remained unchanged (16,504 done,
39 cancelled), and the shared instance settings remained unchanged:
product/customer/fulfillment export and inventory import disabled, webhook fulfillment
auto-validation enabled, unreserved delivery validation disabled, invoicing policy
`manual`.

**Limits and release gates:** Five newly introduced columns (three in `shopify_order`,
two in `shopify_instance`) were not included in the before/after common-column
fingerprints; verify their defaults and semantics separately. Time-stamp changes were
excluded. This is not a live Shopify mutation test, complete business-regression review,
or a production rollback rehearsal. The live `customer_addresses` field mapping was
still `owner=shopify` in the separate read-only production preflight; the agreed
temporary `owner=odoo` policy with `customer_export_enabled=False` is **not yet
applied**. Recheck duplicate Shopify media IDs, relevant queue state, and
export/inventory settings immediately before any approved production upgrade. Keep the
PR in draft until independent review and release approval.

## Earlier isolated validation evidence — 2026-11-21 (Odoo 19.0-20260908)

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
- Current-head CI and a source-level outbound mutation audit later passed at `3a49d51a`;
  independent review, operational safeguards, rollout/rollback planning, and production
  deployment approval remain separate gates.

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
