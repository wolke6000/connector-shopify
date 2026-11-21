# OCA 19.0 integration: three-way merge review and release gates

**Status:** two-parent OCA merge committed in draft integration branch; functional and
database-upgrade review pending. **Updated:** 2026-11-21.

| Revision                        | Commit                                                                      |
| ------------------------------- | --------------------------------------------------------------------------- |
| Switchology baseline (`19.0`)   | `33a308f9774eba7fcc4bf3634397dbe873f0caec`                                  |
| Source integration merge commit | `a6293a1974a3db62a74023b5b91930aca8f50155` (parents `369d90b0`, `5454e91b`) |
| OCA `19.0` integration target   | `5454e91b2f1aa6225026b8cbf6058486607042f8`                                  |
| Common ancestor                 | `64bac8e02a18a19f9b80922d89a009dbcc06eb2f`                                  |

**Three-way blob inventory**: compared all tracked blob paths in the common ancestor,
the original integration branch before the added replay test, and OCA. This is an
**overlap inventory**, not a count of actual Git textual merge conflicts.

- 72 paths changed on OCA only: normally take upstream as-is.
- 5 paths changed on Switchology only: preserve as-is unless upstream changes their
  APIs: `AGENTS.md`, `docs/OCA-DEVIATIONS.md`,
  `shopify_connector/models/field_mapping.py`,
  `shopify_connector/views/shopify_instance_views.xml`,
  `shopify_connector/wizards/product_export.py`.
- 21 paths modified on **both** sides, listed below. Resolve semantically and retain the
  policy tags from `docs/OCA-DEVIATIONS.md`.
- 87 paths identical to the common ancestor.
- Newly introduced OCA module `shopify_connector_website_sale` must be available during
  upgrade, regardless of apparent merge cleanliness.

## Actual Git merge result (2026-11-20)

The read-only GitHub Actions rehearsal performed a genuine
`git merge --no-commit --no-ff` of the pinned OCA SHA, then aborted and saved its
conflict files. It reported **13 actual conflict paths and 41 textual conflict
sections**:

- `shopify_connector/__manifest__.py` (1)
- `shopify_connector/graphql/order.py` (3)
- `shopify_connector/lib/order.py` (1)
- `shopify_connector/models/fulfillment_sync.py` (8)
- `shopify_connector/models/order_binding.py` (1)
- `shopify_connector/models/order_sync.py` (10)
- `shopify_connector/models/product_binding.py` (1)
- `shopify_connector/models/product_sync.py` (1)
- `shopify_connector/tests/__init__.py` (1)
- `shopify_connector/tests/test_fulfillment_inbound.py` (1)
- `shopify_connector/tests/test_fulfillment_sync.py` (2)
- `shopify_connector/tests/test_order_sync.py` (9)
- `shopify_connector/tests/test_product_sync.py` (2)

The resolution was generated locally and independently reproduced in a read-only CI
rehearsal, including Python compilation and duplicate-method detection. A second gated
GitHub Actions run committed the real two-parent merge as `a6293a19` and pushed it to
the **integration branch only**.

**Important semantic choices:**

- Retain the fail-closed historical import guard and its cleanup-replay test; keep
  synced historical bindings eligible for permitted updates.
- Retain independent auto-confirm cutoff, durable order drift ledger, and
  confirmed-order auxiliary-line preservation; remove duplicate shadowing
  implementations introduced by merging separately developed methods.
- Use OCA's new fulfillment retry, tracking, and missed-push architecture, with
  `fulfillment_export_enabled` enforced at enqueue **and** execution.
- Use OCA's relocated image models and `shopify_connector_website_sale`. Database
  upgrade and media ownership preservation passed on an isolated clone on 2026-11-21.
- Preserve the existing fork `.github/workflows/` definitions because the GitHub Actions
  token lacked permission to modify workflow files. Review this infrastructure
  divergence separately.
- Temporary CI merge workflows, the one-off resolution script, and trigger file were
  removed after creating the merge commit.

**This is not a production or CI acceptance.** The merge will need green functional CI,
an isolated neutralized post-cleanup Odoo upgrade, and independent review before PR #17
is ready.

## Overlapping paths: required disposition

| Path (under repository root)                            | Decision and review gate                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `shopify_connector/__manifest__.py`                     | Take OCA module split/version `19.0.1.7.0`; verify dependencies, `website_sale` glue and pre-migration. Do not silently carry old `19.0.1.0.3`.                                                                                                                                                                                                                                                               |
| `shopify_connector/data/ir_cron.xml`                    | Prefer OCA schedules but review activation, ownership, callable, identity and queued side effects before enabling.                                                                                                                                                                                                                                                                                            |
| `shopify_connector/graphql/fulfillment.py`              | Prefer upstream GraphQL shape; preserve order-level fulfillment polling and introspect required Shopify scopes.                                                                                                                                                                                                                                                                                               |
| `shopify_connector/graphql/inventory.py`                | Prefer OCA GraphQL and quantity semantics after testing `write_inventory` scope and product/variant binding flags; never enable bulk export implicitly (SW-OCA-011).                                                                                                                                                                                                                                          |
| `shopify_connector/graphql/order.py`                    | Prefer latest OCA query plus complete address fields; check country-only fallback and Shopify protected-data permission behavior (SW-OCA-007/009).                                                                                                                                                                                                                                                            |
| `shopify_connector/lib/order.py`                        | Prefer OCA normalization/currency/refund changes; retain verified Switchology-specific parsing only when tests demonstrate necessity (SW-OCA-009).                                                                                                                                                                                                                                                            |
| `shopify_connector/models/fulfillment_binding.py`       | Keep `fulfillment_export_enabled=False` default and manual cancel/release guards; retain independent inbound flags (SW-OCA-005/008).                                                                                                                                                                                                                                                                          |
| `shopify_connector/models/fulfillment_sync.py`          | Prefer OCA fulfillment engine, replace rather than duplicate the partial fork backport. Before merge completion guard **all** outbound enqueue+execution paths, including `_push_picking_fulfillment`, `_queue_missed_fulfillment_pushes`, `_queue_shopify_tracking_update`, `_job_update_tracking`, manual release/cancel. Retain inbound stock/carrier safety and auto-confirm cutoff (SW-OCA-002/005/008). |
| `shopify_connector/models/inventory_sync.py`            | Prefer upstream if behavior equivalent; preserve independent product+variant opt-ins and controlled inventory ownership (SW-OCA-011).                                                                                                                                                                                                                                                                         |
| `shopify_connector/models/order_binding.py`             | Preserve `auto_confirm_order_date_from`, `order_drift_pending`, creation-date import settings and related stored-field upgrades (SW-OCA-001/002/003).                                                                                                                                                                                                                                                         |
| `shopify_connector/models/order_sync.py`                | **High risk**. Merge latest OCA order, refund, return, currency and order-line fixes with Switchology creation-date cutoff, drift ledger, incomplete-address and auxiliary-line protections (SW-OCA-001/002/003/006/007/009). Test existing bound historical orders and deleted unbound historical orders separately.                                                                                         |
| `shopify_connector/models/product_binding.py`           | Prefer upstream removal of moved media models, subject to image-binding migration checks; do not lose `shopify.product.image` records (SW-OCA-010).                                                                                                                                                                                                                                                           |
| `shopify_connector/models/product_sync.py`              | Prefer upstream product/media behavior, **reapply fail-closed** `product_export_enabled` in enqueue AND job execution, preserving manual wizard/variant/image guards (SW-OCA-004/010).                                                                                                                                                                                                                        |
| `shopify_connector/tests/__init__.py`                   | Import a single copy of each upstream + local regression test module.                                                                                                                                                                                                                                                                                                                                         |
| `shopify_connector/tests/test_fulfillment_inbound.py`   | Prefer upstream test suite and port Switchology-specific cutoff/export tests rather than retaining duplicated fixtures (SW-OCA-002/005/008).                                                                                                                                                                                                                                                                  |
| `shopify_connector/tests/test_fulfillment_sync.py`      | Keep blocked queued mutations, no external client call, multi-instance tests; integrate new OCA tracking/missed-push cases (SW-OCA-005).                                                                                                                                                                                                                                                                      |
| `shopify_connector/tests/test_order_sync.py`            | Keep cleanup replay/cutoff, drift, address and confirmed auxiliary-line regressions; merge OCA's newer refund and order tests (SW-OCA-001/002/003/006/007/009).                                                                                                                                                                                                                                               |
| `shopify_connector/tests/test_product_sync.py`          | Keep disabled product wizard/queue/job tests; adapt to OCA media glue/ownership (SW-OCA-004/010).                                                                                                                                                                                                                                                                                                             |
| `shopify_connector/views/shopify_fulfillment_views.xml` | Keep inbound-vs-outbound switches visible; no accidental settings defaults changed.                                                                                                                                                                                                                                                                                                                           |
| `shopify_connector/views/shopify_order_views.xml`       | Prefer OCA view additions while retaining necessary cutover/drift status controls.                                                                                                                                                                                                                                                                                                                            |
| `shopify_connector_pos/tests/test_pos.py`               | Keep POS assertions; re-run after upstream routing changes.                                                                                                                                                                                                                                                                                                                                                   |

The **five fork-only paths** also need follow-up after importing OCA: `field_mapping.py`
and product wizard guards may call APIs whose contracts changed; views must load in an
Odoo 19 registry with the new module layout.

## Upstream regression coverage (2026-11-20)

The upstream test method inventory was rechecked against OCA `5454e91b`:

- `test_fulfillment_sync.py`, `test_fulfillment_inbound.py` and `test_product_sync.py`
  retain the upstream test method names.
- Nine previously omitted upstream order methods were restored in `test_order_sync.py`:
  fixed-unit discounts, refunds before invoicing (including partial/full/shipping), and
  delivered-order handled refunds and cancellations (plus their helpers).
- Odoo and OCB **passed** for restored methods at commit `52808357`:
  [GitHub Actions run 44824685850](https://github.com/wolke6000/connector-shopify/actions/runs/44824685850).
- Eight upstream order test/helper method names remain intentionally **not** copied
  verbatim: `_updated_orders_page`,
  `test_order_drift_imports_orders_updated_since_the_last_run`,
  `test_order_drift_skips_orders_already_imported_in_that_version`,
  `test_order_drift_starts_two_days_back_on_its_first_run`,
  `test_order_drift_keeps_its_date_when_shopify_fails`,
  `test_order_drift_also_imports_updated_draft_orders`,
  `test_order_drift_cron_queues_the_updated_orders_sync`, and
  `test_order_drift_search_converts_to_utc`. Switchology has separate durable
  pending-ledger tests whose behavior is not interchangeable with old upstream cursor
  advancement semantics. Additional dedicated tests in `test_order_sync.py` cover (a)
  default two-day first-run window plus one-hour overlap, (b) cron enqueuing of the
  reconciliation job, and (c) conversion of a timezone-aware search timestamp to UTC.
  Odoo/OCB passed these on `0e1e313d` (run `44825434398`). An updated-order drift test
  already verifies pagination and date advancement; tests also cover skipping current
  bindings, API failures and durable re-enqueuing of unacknowledged updates.

  The remaining **semantic** review point is the intentional absence of periodic
  _draft_-order reconciliation: fork bulk import can explicitly queue drafts, but the
  drift scanner only follows regular orders. Confirm with the business owner whether
  this remains desired before closing SW-OCA-003. Do **not** restore old OCA cursor
  semantics merely to match method names.

The 2026-11-21 isolated upgrade and selected regressions do not prove complete live
Shopify write safety, final-head CI, or release readiness.

## Required no-mutation guarantees

For `fulfillment_export_enabled=False` / `product_export_enabled=False`:

1. A completed Odoo picking must not create Shopify fulfillments, even on order reimport
   or replay of a previously queued job.
2. Tracking/carrier edits on completed pickings must not enqueue or execute Shopify
   tracking mutations.
3. Manual fulfillment cancellation and hold-release must not queue or execute Shopify
   writes.
4. Odoo product/variant/pricelist/image changes must not export products, including
   manually queued jobs or wizard-driven calls.
5. Inventory export stays **independent** and can only run for intentionally enabled
   product **and variant** bindings with reviewed location mapping.
6. Shopify inbound fulfillment import must not purchase a second carrier label.
7. The historical date cutoff must reject orders deleted during the 2026-11-19 cleanup,
   regardless of new webhook `updatedAt`, while permitting updates to pre-existing
   **synced** historical bindings.
8. Auto-confirm cutoff protects draft historical orders independently of import cutoff.
9. No automatic customer invoice/payment posting in Switchology.

See `docs/OCA-DEVIATIONS.md` for the source policy IDs. Implement each upstream outbound
guard in code and test it **before creating a merge commit**.

## Technical workflow and acceptance

1. Fetch the **exact** OCA SHA and the current integration branch; verify clean
   worktree, module paths, pin and `AGENTS.md`. Work on integration branch only.
2. Perform an actual Git **three-way merge** (normal merge commit with the original fork
   commit and OCA commit as parents), not a file overwrite or synthetic reconstruction
   that masks conflicts.
3. Preserve OCA-only changes, classify every overlapping file, and resolve all conflicts
   semantically. No conflict markers, duplicate methods or skipped imports.
4. Compare merged result with OCA: review **remaining** differences and update
   `docs/OCA-DEVIATIONS.md` and matching `SW-OCA-###` code comments.
5. Run pre-commit, Odoo/OCB CI, mutation-negative tests, cutoffs, drift, confirmed
   order/refund, multi-step/kit fulfillment and ledger/idempotency tests.
6. On a **network-isolated neutralized post-cleanup DB+filestore** with no Shopify
   credentials/egress and queue/cron disabled, test `-u shopify_connector` plus
   installation/migration of `shopify_connector_website_sale`. Verify image bindings,
   schema and field defaults. Never run `-u all` on production.
7. Independently review the diff, operational impact, migration evidence and rollback
   before opening a ready-for-review integration PR. Merge of this branch is not
   authority to change `odoo-switchology` submodule pin or run production deployment.

### Verification evidence and remaining gates

- The integration branch has policy comments and `docs/OCA-DEVIATIONS.md`.
- The branch includes `test_cutover_blocks_reimport_of_deleted_historical_binding` and
  restored OCA order regressions. Odoo/OCB fresh-install CI passed at `52808357` (run
  `44824685850`). This is not the clone-upgrade test.
- OCA source merge commit `a6293a19` is confirmed. An isolated Odoo module upgrade
  passed on 2026-11-21; no production change is authorized.

## Follow-up validation — 2026-11-21

- Isolated post-cleanup schema migration passed from `19.0.1.0.3` to `19.0.1.7.0` with
  glue-addon install; 127 media bindings and 38 variant relationships survived, with
  constraints and ORM smoke checks.
- Twenty-one targeted tests passed on a separate isolated Odoo 19.0-20260908 clone: five
  order/change/cancellation, six outbound fulfillment, five inbound fulfillment, and
  five refund regressions (zero failures/errors).
- Independent mocked product import/idempotency/image-prune/export-guard and historical
  cutoff smoke checks passed with rollback.
- Snapshot inventory flags and 36 opt-in variant bindings were checked; one active
  Shopify location maps to `WH/Stock`. Later read-only live production preflight
  confirmed product/customer/fulfillment exports and inventory import disabled,
  webhook fulfillment auto-validation enabled, one active mapped location, no
  duplicated Shopify media IDs and no pending/enqueued/started queue jobs. No
  production settings were changed.
- Draft-order periodic drift reconciliation is consciously deferred; it is not asserted
  to be implemented or validated by these tests.
- A source-level Shopify outbound mutation audit was completed at `3a49d51a`,
  including product/customer/fulfillment guards, intentional Odoo-to-Shopify inventory
  updates, and operator-triggered administrative webhook mutations. The audit does
  not prove live behavior for old queued jobs or multi-instance configurations.
- GitHub Actions pre-commit and Odoo/OCB tests both passed at `3a49d51a`:
  runs `44931372890` and `44931372929` (359 tests per Odoo/OCB suite). Subsequent
  documentation commits need fresh CI verification.
- Still open: independent human review, newly introduced field defaults, deployment
  preflight, operational replay/write guards, explicit rollout/rollback planning, and
  separate production deployment authorization.

## Additional migration-history evidence

The earlier CP2A migration rehearsal **failed** schema acceptance: 143 image bindings
and 42 image/variant links existed, with seven media IDs shared across five separate
Shopify products. OCA's new unique index on `(instance_id, shopify_id)` could not be
created, although Odoo exited 0. The old fork index also included `template_binding_id`.
This was not a missing-media or lost-binding issue, and must not be described as a
successful migration. The media assignments were subsequently changed in Shopify.

The successfully migrated `39689e91` production-derived snapshot was a **different,
later input**: 79 sale orders, 85 Shopify order bindings, 127 media bindings, 38 variant
relationships and zero shared media keys before upgrade. All 127/38 survived; the
intended OCA index and module ownership were verified. The successful test does not
establish that arbitrary legacy stores containing shared media IDs can upgrade
unchanged. Preflight and postflight checks for the unique index are release-critical.

At commit `0ad9144654f439502e25fe451a2b1286d5947f4b`, GitHub Actions pre-commit
(run 44906525072) and Odoo/OCB tests (run 44906525078) passed. This CI result applies to
that exact SHA, not automatically to later commits.

## Production-version re-rehearsal — 2026-11-21

**PASS for the cleaned production-derived snapshot `39689e91` and tested
connector commit `3a49d51a`.** A fresh pre-upgrade PostgreSQL 15 restore and
matching filestore were prepared on an internal Docker-only network. A separate
pre-upgrade dump was preserved after discovering (but not installing) the new glue
addon. Odoo's exact production **release** `19.0-20261102` was used with
`openupgradelib==3.13.7` on AMD64; production runs ARM64. Odoo parent source
was pinned to `7ce4eaaa`, with all four other OCA submodule commits matching
production. This is not a byte-identical production ARM64 image test.

- The isolated core upgrade exited 0, generated zero warning/error log matches,
  installed `shopify_connector=19.0.1.7.0` and
  `shopify_connector_website_sale=19.0.1.0.0`.
- PostgreSQL `shopify_product_image_instance_gid_unique` is actually present,
  valid, and enforces `UNIQUE(instance_id, shopify_id)`. No duplicate media keys;
  correct XML-ID/constraint/relation ownership; no missing or duplicate links.
- Odoo's loaded ORM registry returned 127 Shopify image bindings, 38 image/variant
  relations, 54 product templates, 83 variants, 85 Shopify order bindings and 79
  sale orders.
- A separately restored **before** checkpoint and **after** database produced
  equal ordered row-content fingerprints for **all shared columns other than
  `create_date`/`write_date`** across 13 tables (including orders/lines,
  customer partners, stock quants, queue jobs, product/media bindings and instance
  settings). Results: **13/13 PASS; 0 deviations.** Full per-table counts and
  limits: [database migration evidence](OCA-DATABASE-UPGRADE-VALIDATION.md).
- Five new columns (three Shopify-order, two instance) were not included in the
  shared-column fingerprints and require separate default/semantic review.
- No production/Shopify changes occurred. The temporary `customer_addresses=odoo`
  policy is still **not applied to production**; a separate approved settings step
  and release/rollback plan remain open. The PR must remain Draft until review.
