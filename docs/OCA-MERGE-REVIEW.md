# OCA 19.0 integration: three-way merge review and release gates

**Status:** pre-merge assessment (no OCA merge commit, no integration test executed).
**Updated:** 2026-11-20.

| Revision | Commit |
|---|---|
| Switchology baseline (`19.0`) | `33a308f9774eba7fcc4bf3634397dbe873f0caec` |
| Integration branch (documented policy and regression test) | Check branch HEAD at execution; do not rely on this file for a moving SHA |
| OCA `19.0` integration target | `5454e91b2f1aa6225026b8cbf6058486607042f8` |
| Common ancestor | `64bac8e02a18a19f9b80922d89a009dbcc06eb2f` |

**Three-way blob inventory**: compared all tracked blob paths in the common ancestor,
the original integration branch before the added replay test, and OCA. This is
an **overlap inventory**, not a count of actual Git textual merge conflicts.

- 72 paths changed on OCA only: normally take upstream as-is.
- 5 paths changed on Switchology only: preserve as-is unless upstream changes their APIs:
  `AGENTS.md`, `docs/OCA-DEVIATIONS.md`,
  `shopify_connector/models/field_mapping.py`,
  `shopify_connector/views/shopify_instance_views.xml`,
  `shopify_connector/wizards/product_export.py`.
- 21 paths modified on **both** sides, listed below. Resolve semantically and
  retain the policy tags from `docs/OCA-DEVIATIONS.md`.
- 87 paths identical to the common ancestor.
- Newly introduced OCA module `shopify_connector_website_sale` must be available
  during upgrade, regardless of apparent merge cleanliness.

## Overlapping paths: required disposition

| Path (under repository root) | Decision and review gate |
|---|---|
| `shopify_connector/__manifest__.py` | Take OCA module split/version `19.0.1.7.0`; verify dependencies, `website_sale` glue and pre-migration. Do not silently carry old `19.0.1.0.3`. |
| `shopify_connector/data/ir_cron.xml` | Prefer OCA schedules but review activation, ownership, callable, identity and queued side effects before enabling. |
| `shopify_connector/graphql/fulfillment.py` | Prefer upstream GraphQL shape; preserve order-level fulfillment polling and introspect required Shopify scopes. |
| `shopify_connector/graphql/inventory.py` | Prefer OCA GraphQL and quantity semantics after testing `write_inventory` scope and product/variant binding flags; never enable bulk export implicitly (SW-OCA-011). |
| `shopify_connector/graphql/order.py` | Prefer latest OCA query plus complete address fields; check country-only fallback and Shopify protected-data permission behavior (SW-OCA-007/009). |
| `shopify_connector/lib/order.py` | Prefer OCA normalization/currency/refund changes; retain verified Switchology-specific parsing only when tests demonstrate necessity (SW-OCA-009). |
| `shopify_connector/models/fulfillment_binding.py` | Keep `fulfillment_export_enabled=False` default and manual cancel/release guards; retain independent inbound flags (SW-OCA-005/008). |
| `shopify_connector/models/fulfillment_sync.py` | Prefer OCA fulfillment engine, replace rather than duplicate the partial fork backport. Before merge completion guard **all** outbound enqueue+execution paths, including `_push_picking_fulfillment`, `_queue_missed_fulfillment_pushes`, `_queue_shopify_tracking_update`, `_job_update_tracking`, manual release/cancel. Retain inbound stock/carrier safety and auto-confirm cutoff (SW-OCA-002/005/008). |
| `shopify_connector/models/inventory_sync.py` | Prefer upstream if behavior equivalent; preserve independent product+variant opt-ins and controlled inventory ownership (SW-OCA-011). |
| `shopify_connector/models/order_binding.py` | Preserve `auto_confirm_order_date_from`, `order_drift_pending`, creation-date import settings and related stored-field upgrades (SW-OCA-001/002/003). |
| `shopify_connector/models/order_sync.py` | **High risk**. Merge latest OCA order, refund, return, currency and order-line fixes with Switchology creation-date cutoff, drift ledger, incomplete-address and auxiliary-line protections (SW-OCA-001/002/003/006/007/009). Test existing bound historical orders and deleted unbound historical orders separately. |
| `shopify_connector/models/product_binding.py` | Prefer upstream removal of moved media models, subject to image-binding migration checks; do not lose `shopify.product.image` records (SW-OCA-010). |
| `shopify_connector/models/product_sync.py` | Prefer upstream product/media behavior, **reapply fail-closed** `product_export_enabled` in enqueue AND job execution, preserving manual wizard/variant/image guards (SW-OCA-004/010). |
| `shopify_connector/tests/__init__.py` | Import a single copy of each upstream + local regression test module. |
| `shopify_connector/tests/test_fulfillment_inbound.py` | Prefer upstream test suite and port Switchology-specific cutoff/export tests rather than retaining duplicated fixtures (SW-OCA-002/005/008). |
| `shopify_connector/tests/test_fulfillment_sync.py` | Keep blocked queued mutations, no external client call, multi-instance tests; integrate new OCA tracking/missed-push cases (SW-OCA-005). |
| `shopify_connector/tests/test_order_sync.py` | Keep cleanup replay/cutoff, drift, address and confirmed auxiliary-line regressions; merge OCA's newer refund and order tests (SW-OCA-001/002/003/006/007/009). |
| `shopify_connector/tests/test_product_sync.py` | Keep disabled product wizard/queue/job tests; adapt to OCA media glue/ownership (SW-OCA-004/010). |
| `shopify_connector/views/shopify_fulfillment_views.xml` | Keep inbound-vs-outbound switches visible; no accidental settings defaults changed. |
| `shopify_connector/views/shopify_order_views.xml` | Prefer OCA view additions while retaining necessary cutover/drift status controls. |
| `shopify_connector_pos/tests/test_pos.py` | Keep POS assertions; re-run after upstream routing changes. |

The **five fork-only paths** also need follow-up after importing OCA:
`field_mapping.py` and product wizard guards may call APIs whose contracts changed;
views must load in an Odoo 19 registry with the new module layout.

## Required no-mutation guarantees

For `fulfillment_export_enabled=False` / `product_export_enabled=False`:

1. A completed Odoo picking must not create Shopify fulfillments, even on order
   reimport or replay of a previously queued job.
2. Tracking/carrier edits on completed pickings must not enqueue or execute Shopify
   tracking mutations.
3. Manual fulfillment cancellation and hold-release must not queue or execute Shopify writes.
4. Odoo product/variant/pricelist/image changes must not export products, including
   manually queued jobs or wizard-driven calls.
5. Inventory export stays **independent** and can only run for intentionally enabled
   product **and variant** bindings with reviewed location mapping.
6. Shopify inbound fulfillment import must not purchase a second carrier label.
7. The historical date cutoff must reject orders deleted during the 2026-11-19
   cleanup, regardless of new webhook `updatedAt`, while permitting updates to
   pre-existing **synced** historical bindings.
8. Auto-confirm cutoff protects draft historical orders independently of import cutoff.
9. No automatic customer invoice/payment posting in Switchology.

See `docs/OCA-DEVIATIONS.md` for the source policy IDs. Implement each upstream
outbound guard in code and test it **before creating a merge commit**.

## Technical workflow and acceptance

1. Fetch the **exact** OCA SHA and the current integration branch; verify clean
   worktree, module paths, pin and `AGENTS.md`. Work on integration branch only.
2. Perform an actual Git **three-way merge** (normal merge commit with the original
   fork commit and OCA commit as parents), not a file overwrite or synthetic
   reconstruction that masks conflicts.
3. Preserve OCA-only changes, classify every overlapping file, and resolve all
   conflicts semantically. No conflict markers, duplicate methods or skipped imports.
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

### Current evidence

- The integration branch has policy comments and `docs/OCA-DEVIATIONS.md`.
- The branch now includes `test_cutover_blocks_reimport_of_deleted_historical_binding`;
  it has **not** been exercised against an Odoo runtime in this task.
- No OCA merge or module upgrade is claimed by this assessment. No production
  changes are authorized.
