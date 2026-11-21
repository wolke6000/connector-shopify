# Switchology deviations from OCA connector-shopify (Odoo 19)

> **Status: OCA source merged into a DRAFT branch; behavioral/upgrade verification
> pending.** Last reviewed: 2026-11-20. Switchology fork baseline:
> `33a308f9774eba7fcc4bf3634397dbe873f0caec` (`19.0`). OCA upstream reference:
> `5454e91b2f1aa6225026b8cbf6058486607042f8` (`19.0`). Merge base:
> `64bac8e02a18a19f9b80922d89a009dbcc06eb2f`. At baseline, fork was 37 commits ahead /
> 188 behind upstream; 25 files differed. Update these references after each upstream
> integration or meaningful policy change. Integration merge commit:
> `a6293a1974a3db62a74023b5b91930aca8f50155` (parents: integration branch `369d90b0` and
> OCA `5454e91b`). This source integration has now passed an isolated 2026-11-21 schema
> upgrade; it is not deployed. The isolated 2026-11-21 migration passed; production
> rollout remains separate. Post-merge diff against OCA has to be reviewed after
> follow-up fixes; do not interpret the baseline 25-file inventory as the final
> difference count.

## Purpose and conventions

The goal is **minimal permanent divergence** from `OCA/connector-shopify:19.0`. Document
each intentionally retained behavioral difference here, and mark its implementation with
a matching short comment: `# SW-OCA-###: reason; see docs/OCA-DEVIATIONS.md`. Comments
identify **policy/behavior boundaries**, not every internal helper or every line. Never
paste this entire table into source files.

Every deviation requires: ID, upstream behavior, Switchology behavior, business reason,
location(s), test evidence, and a disposition (`KEEP`, `UPSTREAM`, `REMOVE`, `REVIEW`).
If a difference becomes identical to upstream, delete the code marker and close or move
the ledger entry to the resolved section. `KEEP` does not mean the implementation must
stay fork-specific forever; revisit available OCA options first.

### Required safety boundaries

Shopify is authoritative for customer sales/payment/invoicing, storefront product
content, shipping and fulfillment. Odoo sale orders exist for internal stock demand and
MRP. A confirmed Odoo order/picking does **not** authorize a Shopify fulfillment.

Any OCA merge must retain historical creation-date and auto-confirm cutoffs,
incomplete-address protection, queue/drift durability, and outbound mutation safeguards.
Verify by **tests** rather than by presence of comments alone.

## Deliberate or provisional deviations

| ID             | Status                                                           | Upstream baseline vs. Switchology behavior                                                                                                                                                                                                                                                                                                                                                                                    | Why / affected code                                                                                                                                                                                                                                                                | Release and test gate                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| -------------- | ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **SW-OCA-001** | **KEEP**                                                         | Fork rejects creation/retry of old unbound or failed Shopify orders based on **Shopify creation date**, even when an update webhook arrives later; already-synced historical orders can update. OCA's importer has no equivalent fail-closed cutoff at the shared import job.                                                                                                                                                 | `models/order_sync.py::_job_import_order`; `models/order_binding.py::order_import_date_from`. Prevent recreation of the 270 removed historical sale orders / 281 bindings after the 2026-11-19 one-off cleanup.                                                                    | Test webhook/API/bulk/drift replay; missing and invalid creation timestamps; synced older bindings update; no duplicate recreation. Do **not** remove after cleanup.                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| **SW-OCA-002** | **KEEP**                                                         | Separate Shopify creation-time auto-confirm cutoff, beyond regular payment eligibility.                                                                                                                                                                                                                                                                                                                                       | `models/order_binding.py::auto_confirm_order_date_from`, `models/order_sync.py::_is_after_confirmation_cutoff` and `_should_confirm_shopify_order`; also inbound order confirmation paths. Protect old technical demand documents from unexpected stock procurement/confirmations. | Verify drafts, paid/partially-paid updates, inbound fulfillment, retained historical bindings, and existing instance timestamp on a restored DB.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| **SW-OCA-003** | **KEEP — revisit when upstream provides durable acknowledgment** | Fork maintains a durable `order_drift_pending` ledger and retries updates not acknowledged after the reconciliation cursor advances. Checked OCA `5454e91b`: upstream advances `order_drift_since` after enqueueing jobs and has no persisted pending acknowledgement; an import not executed or permanently failing may fall outside later search windows. Fork also deliberately keeps draft-order reconciliation separate. | `models/order_binding.py::order_drift_pending`, `models/order_sync.py::_job_sync_updated_orders` and success/acknowledgment logic.                                                                                                                                                 | Retain the fork override and existing retry/failure regression coverage. Reassess newer OCA releases periodically for equivalent persistent acknowledgment and explicitly controlled draft reconciliation; do not drop the ledger merely to reduce diff size.                                                                                                                                                                                                                                                                                                                                                                               |
| **SW-OCA-004** | **KEEP**                                                         | Product, price, image and metadata mutations are fail-closed when instance `product_export_enabled=False`, including enqueue, manual wizard and execution of old jobs. Independent of inventory export. OCA reference has no equivalent global flag.                                                                                                                                                                          | `models/field_mapping.py`, `models/product_sync.py`, `wizards/product_export.py`, `views/shopify_instance_views.xml`; added by fork PR #13.                                                                                                                                        | Test both queued and manually invoked mutations, multi-instance flags, and the explicit enabled case. New field needs isolated DB module-upgrade test; confirm existing instances initialize it false.                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| **SW-OCA-005** | **KEEP**                                                         | Fulfillment mutations are fail-closed when `fulfillment_export_enabled=False`: outgoing picking enqueue/job, manual hold release/cancel and queued executions. New instances default false. OCA reference lacks this switch and includes **additional** outbound paths.                                                                                                                                                       | `models/fulfillment_binding.py`, `models/fulfillment_sync.py`; fork PR #13.                                                                                                                                                                                                        | During OCA integration explicitly guard **`_push_picking_fulfillment`, `_queue_missed_fulfillment_pushes`, `_queue_shopify_tracking_update`, `_job_update_tracking`**, and all external mutation entry points, at enqueue **and** job execution. Also block `models/operability.py::action_retry_sync` / `_shopify_enqueue_fresh_retry` before resetting failed outgoing fulfillment bindings; keep their original error when exports are disabled. Test per-instance and already queued jobs.                                                                                                                                              |
| **SW-OCA-006** | **REVIEW / likely KEEP until upstream equivalent**               | Fork preserves duty, shipping and subtotal-adjustment auxiliary lines on confirmed orders by setting quantity/amount to zero rather than unlinking. Base OCA retained removed ordinary product lines; exact auxiliary-line equivalence needs reconciliation.                                                                                                                                                                  | `models/order_sync.py::_remove_auxiliary_lines` and auxiliary helpers; fork PRs #14 and #15.                                                                                                                                                                                       | Confirmed Odoo 19 orders, refunds, omitted shipping/duties, duplicate lines, reactivation, identity and idempotency; do not introduce deletions on confirmed orders.                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| **SW-OCA-007** | **REVIEW**                                                       | Fork protects existing shipping/billing contacts when Shopify returns country-only or incomplete order-specific addresses; newer orders can include fully approved protected customer data.                                                                                                                                                                                                                                   | `models/order_sync.py::_build_order` partner selection; fork PRs #8/#11, order normalization.                                                                                                                                                                                      | Confirm semantics against OCA's new address logic. Test both incomplete historical imports and complete newer payloads; never synthesize addresses or confuse customer defaults with order-specific addresses.                                                                                                                                                                                                                                                                                                                                                                                                                              |
| **SW-OCA-008** | **UPSTREAM merged; test pending**                                | Fork PR #16 ported much of upstream inbound fulfillment validation, including partial/multi-step/kit handling and carrier-booking suppression.                                                                                                                                                                                                                                                                                | `models/fulfillment_sync.py`, `graphql/fulfillment.py`, `tests/test_fulfillment_inbound.py`.                                                                                                                                                                                       | Prefer upstream implementation after semantic merge; preserve independent `fulfillment_auto_validate_webhook` and `fulfillment_validate_unreserved` settings and SW-OCA-002/005. No duplicate implementation.                                                                                                                                                                                                                                                                                                                                                                                                                               |
| **SW-OCA-009** | **KEEP**                                                         | Other altered order normalization, currency/tax/rounding, SKU and refund logic is not yet classified line-by-line.                                                                                                                                                                                                                                                                                                            | `graphql/order.py`, `lib/order.py`, `models/order_sync.py`, tests.                                                                                                                                                                                                                 | Trace each remaining diff against upstream and tests. Prefer OCA's newer fixes; keep only a documented demonstrated behavior. No blanket 'keep all fork fixes'.                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| **SW-OCA-010** | **Source integrated; DB upgrade gate OPEN**                      | OCA `19.0.1.7.0` removes `website_sale` from the main connector dependencies and adds `shopify_connector_website_sale` plus migration `19.0.1.1.0/pre-migration.py`. Fork is still `19.0.1.0.3`, with media bindings in the base module.                                                                                                                                                                                      | Module manifest, product-image models/controllers, migration and glue module. This is a **schema/module migration**, not a mere source diff.                                                                                                                                       | Isolated post-cleanup restore; verify module available and pre-migration succeeds; preserve product-image bindings. OCA migration raises if image bindings exist and glue module is unavailable. Never trial directly on production.                                                                                                                                                                                                                                                                                                                                                                                                        |
| **SW-OCA-011** | **REVIEW / INVENTORY IMPORT GATE OPEN**                          | Inventory export controls use independent product+variant opt-ins and location mapping. OCA 19.0 inventory **import** still applies Shopify `available` directly to Odoo stock, potentially double-counting a sale once from inventory webhook and again upon delivery validation. The Odoo 18 fix from [OCA PR #67](https://github.com/OCA/connector-shopify/pull/67) is **not integrated into 19.0**.                       | `models/inventory_sync.py::_handle_inventory_level_update`, `models/inventory_binding.py::inventory_import_enabled`, `tests/test_inventory.py`, `docs/INVENTORY-IMPORT-SAFETY-GATE.md`.                                                                                            | Keep `inventory_import_enabled=False` (default) unless separately approved; verify actual production flag and isolated upgrade preservation. Field `copy=False`, explicit `action_duplicate_configuration` default `False`, and server-side onboarding refusal for `bidirectional` prevent inherited/normal-wizard activation; tests in `test_instance.py`, `test_operability.py`, and `test_inventory.py`. Negative tests assert disabled webhook makes no stock/API writes. Active-import double-count fix and tests remain a **separate release gate**, not authorized by PR #17. Outbound inventory export controls remain independent. |

These entries classify **known functional deviations and migration risks**, not all 25
baseline changed files. Differences in tests, translations, package versions and display
metadata still require normal merge review; never assume undocumented diffs are
approved.

## Source integration outcome (2026-11-20)

- Real two-parent merge commit `a6293a1974a3db62a74023b5b91930aca8f50155`. OCA
  `5454e91b` is an ancestor of the integration branch; the 188 previous missing upstream
  commits are now in its history.
- An actual `git merge --no-commit` reported **13 textual conflict files** (41 conflict
  regions). The remaining overlapping paths merged automatically. The decision log is
  `docs/OCA-MERGE-REVIEW.md`.
- The OCA media glue module `shopify_connector_website_sale` and the upstream
  pre-migration were added to source. An isolated 2026-11-21 database upgrade passed;
  production has not been changed.
- `SW-OCA-004` and `SW-OCA-005` are retained as explicit outbound write safeguards.
  Newly introduced OCA missed-fulfillment and tracking mutation paths are guarded for
  disabled exports; regression verification is still required.
- `SW-OCA-001`, `SW-OCA-002`, `SW-OCA-003`, `SW-OCA-006`, and `SW-OCA-007` have
  provisional merged implementations; their release behavior is **not yet accepted**.
- The base fork's `.github/workflows/` definitions are intentionally retained because
  the GitHub Actions app token cannot update workflow files. The OCA workflow
  improvements remain a **separate permissions/review task**.
- Merge-candidate Python syntax and duplicate-class-method checks passed in the offline
  integration rehearsal. This does not validate Odoo behavior. GitHub CI and isolated
  Odoo database upgrade remain independent release gates.
- No Odoo main/submodule production pin or live Shopify configuration changed.

## Baseline changed-file inventory (fork relative to OCA 19.0)

`AGENTS.md`; `shopify_connector/__manifest__.py`; `shopify_connector/data/ir_cron.xml`;
`shopify_connector/graphql/{fulfillment,inventory,order}.py`;
`shopify_connector/lib/order.py`;
`shopify_connector/models/{field_mapping,fulfillment_binding,fulfillment_sync,inventory_sync,order_binding,order_sync,product_binding,product_sync}.py`;
`shopify_connector/tests/{__init__,test_fulfillment_inbound,test_fulfillment_sync,test_order_sync,test_product_sync}.py`;
`shopify_connector/views/{shopify_fulfillment_views,shopify_instance_views,shopify_order_views}.xml`;
`shopify_connector/wizards/product_export.py`;
`shopify_connector_pos/tests/test_pos.py`.

Review every changed file after merge and update the table above for each **remaining
behavior**.

## Integration procedure / evidence

1. **Merge, don't overwrite or force-push:** integrate pinned OCA `19.0` into this
   dedicated fork branch with semantic conflict review.
2. Record upstream SHA, fork parent SHA, merge commit, diff inventory, and any newly
   introduced Shopify mutations.
3. For each retained `SW-OCA-###`, map to the smallest implementation location and add
   the matching source comment; avoid comments in untouched OCA paths.
4. Reject regressions in historical import cutoff, auto-confirm cutoff, drift
   durability, order lines, incomplete addresses, invoicing policy, inventory opt-in,
   and Shopify write guards.
5. Run pre-commit and Odoo/OCB integration tests **including both allowed and blocked
   paths**. Tests should assert external client methods/mutations are not called with
   export disabled.
6. Test OCA schema migration with isolated, neutralized **post-cleanup** DB + filestore,
   no live Shopify access, and controlled module upgrade. Record counts/invariants, and
   don't infer deployment from green CI.
7. Only then open/review an integration PR against fork `19.0`. Production Odoo is
   separately pinned via `wolke6000/odoo-switchology`; changing this branch alone
   changes neither production nor Odoo's gitlink.

### Review gate for each future fork-only deviation

Add a ledger row with: **ID → exact entry points → upstream alternative / commit →
business constraint → tests → migration effects → decision and reviewer**. The PR must
state why the deviation cannot be expressed with upstream configuration or contributed
upstream. A new policy without this entry and nearby code marker is incomplete.

## Resolved/upstreamed items

_Upstream code was integrated (not yet verified by Odoo tests). Move individual IDs here
only after behavior and upgrade tests pass; do not infer resolved status from a
successful Git merge._

## Snapshot validation update (2026-11-21)

- Isolated migrated schema preserved 127 image bindings and 38 image/variant relations,
  with new media ownership and Odoo registry checks passing.
- Twenty-one targeted tests passed on isolated Odoo 19.0-20260908. Coverage includes
  orders, cancellation, outbound guards, inbound deliveries, and refunds; it does not
  replace the full Odoo/OCB suite at the final HEAD.
- Snapshot inventory import and product/customer/fulfillment export were disabled; 22
  product and 36 tracked variant bindings opted into inventory export. All four tested
  binding-consistency anomalies were zero; one active mapped location points to internal
  `WH/Stock`.
- `SW-OCA-011` inventory import remains **not approved**; these checks do not validate
  enabled inbound-stock semantics or production configuration.
- Periodic draft-order drift reconciliation is deferred by business decision; do not
  silently claim equivalent upstream draft reconciliation.
- Remaining gates: full outbound mutation audit, independent code review, final-head
  pre-commit/Odoo/OCB, and release/rollback authorization.

## Media migration edge case (2026-11-21)

The older CP2A clone had 143 image bindings, 42 variant relations and seven
cross-product repeated Shopify media IDs (20 records, 13 additional bindings). OCA's
`UNIQUE(instance_id, shopify_id)` index failed to create during the first isolated
upgrade, even though the Odoo process returned exit code 0. This is a **real schema
migration failure** for that input, not a reason to silently delete bindings or relax
the upstream constraint. Shopify inspection in the prior review confirmed genuine shared
media IDs between separate products. The merchant subsequently changed Shopify media
assignments.

The newer `39689e91` snapshot had no repeated media IDs and its migration passed without
connector source changes: 127 image bindings and 38 variant relations preserved, with
correct unique index and glue-module ownership. This does not resolve the general
cross-product-media behavior; preflight must reject or explicitly handle any recurrence.
Document and review any future source-level divergence from OCA before implementing it.

## Temporary customer address ownership policy (2026-11-21)

**Business decision:** Prefer upstream configuration over a fork-only change. For
Switchology, use `customer_addresses=odoo` **together with**
`customer_export_enabled=False`. This is a temporary operational policy, **not** a
change to the OCA customer importer.

- **Current state not yet changed:** The isolated production-derived snapshot showed
  `customer_addresses=shopify`; customer export was disabled. Recheck actual live
  settings and separately authorize the ownership change before deployment. Do not claim
  the snapshot or production was modified.
- **Why:** With Shopify-owned customer addresses, a subsequent Shopify customer
  MailingAddress update was observed to modify the same Odoo `res.partner` referenced by
  a confirmed historical order (red-before test on `9000b65b`). This behavior is
  byte-identical to the integrated OCA upstream, not a merge artifact.
- **Verified mitigation:** With Odoo-owned customer addresses, a new customer still
  receives an initial Shopify address seed but subsequent customer MailingAddress
  updates do not alter the already referenced Odoo contact. Test
  `test_odoo_owned_customer_address_preserves_confirmed_order_address` passed under Odoo
  and OCB (359 tests each, zero failures) at `72fb89b0`; pre-commit passed.
- **Trade-off:** Odoo will no longer continuously mirror subsequent edits to Shopify's
  customer address book. Order-specific shipping/billing addresses continue to be
  imported from each Shopify order independently. `customer_addresses=odoo` alone does
  **not** forbid exports; retain and verify `customer_export_enabled=False`, including
  preexisting queue jobs.
- **Revisit:** When OCA upstream supports preserving historical order-linked address
  partners while refreshing Shopify-owned customer addresses, assess its regression
  coverage and migration impact. If satisfactory, consider switching ownership back to
  `shopify` to restore continuous inbound customer address updates, without a
  Switchology-specific implementation.

## Review decisions: inventory direction and Shopify accounting (2026-11-21)

### HIGH-01: default inventory export eligibility — accepted

- Keep OCA defaults `inventory_sync_enabled=True` for new Shopify product template and
  variant bindings. This is deliberate: a newly imported Shopify product should create
  Odoo product/variant bindings with Shopify InventoryItem links, then synchronize
  **Odoo stock to Shopify** where tracking, storable status, mapping and other existing
  eligibility guards permit it.
- Do not disable existing inventory synchronization or introduce an additional
  per-product opt-in by default. Respect existing per-binding opt-outs.
- The inventory export path uses Shopify `inventorySetQuantities`; it is distinct from
  importing Shopify stock into Odoo. Deployment must verify warehouse/location mappings
  and the intended source-of-truth quantities.
- **Decision:** accept existing upstream behavior; no fork change for HIGH-01.

### HIGH-02: inbound Shopify stock webhook — disabled

- Keep `shopify_instance.inventory_import_enabled=False`. This flag is independent of
  importing Shopify products and InventoryItem identifiers.
- The OCA `inventory_levels/update` handler returns immediately when this flag is off.
  When enabled, it computes a delta against Odoo stock and calls
  `stock.quant._update_available_quantity`, so bidirectional inventory authority could
  introduce unintended stock adjustments.
- The handler is identical to the inspected OCA upstream version. The isolated
  production-derived snapshot has inventory import disabled, but the live production
  configuration must be rechecked before deployment.
- **Decision:** no fork change for HIGH-02; enabling inbound quantity writes requires a
  separate design review, tests and explicit authorization.

### HIGH-04: automatic Odoo credit notes — ACCEPTED (operational policy)

- `order_invoicing_policy=manual` stops automatic invoice creation but does **not**
  prevent a posted Odoo credit note on Shopify refund import if a posted `out_invoice`
  is already linked to that Odoo sale order. This path also exists in the inspected OCA
  upstream version.
- Read-only SQL on the isolated production-derived snapshot reported
  `order_invoicing_policy=manual`, `refund_uninvoiced_policy=adjust`, **zero** Shopify
  orders linked to posted Odoo invoices and **zero** matching posted Odoo invoices.
  Therefore the known credit-note path had no qualifying invoices in that snapshot, but
  the risk is not eliminated for future manually posted invoices.
- Shopify is authoritative for invoicing, payments and credit notes. Do not manually
  post Odoo customer invoices for Shopify-linked sale orders. Recheck both the policy
  and posted-invoice count on live production before deployment. If a posted invoice
  appears, stop and separately resolve the credit-note risk before processing further
  Shopify refunds.
- **Decision (2026-11-21): ACCEPTED — operational policy; not a PR #17 code/merge
  blocker.** Shopify creates and manages customer invoices and credit notes. Switchology
  will not post Odoo customer invoices linked to Shopify orders. Keep
  `order_invoicing_policy=manual`; do not add a fork-specific refund patch. The
  exception remains: a manually posted linked Odoo invoice could still trigger an
  automatic Odoo credit note, so the live preflight and stop condition above remain
  mandatory for deployment. Reassess when upstream provides an equivalent accounting
  guard or business requirements change.

These decisions do not approve production configuration changes, deployment or Shopify
writes.

## SW-OCA-009: targeted normalization and order processing review (2026-11-21)

**Decision: KEEP the documented Switchology-only order exceptions; use OCA for
equivalent payment, refund, return, currency and total-validation methods.** This
classification is based on source comparison with OCA `5454e91b` and successful
pre-commit, Odoo and OCB workflows at `338c41e7`.

- `lib/order.py` additionally normalizes `currentTotalDutiesSet`, queried by
  `graphql/order.py`. The GraphQL updated-order search also requests `createdAt` for the
  historical import cutoff.
- Duty and subtotal-adjustment lines preserve Shopify amounts, with a maximum
  one-minor-currency-unit correction when no discount exists. Related confirmed-order
  and rounding regression tests are retained.
- When Shopify omits its variant ID, a uniquely matching Odoo SKU can resolve historical
  line items. The explicit `SHOPIFY-LEGACY-CUSTOM` fallback covers lines without
  product, variant and SKU identifiers. Switchology operates one company; do not
  introduce a speculative multi-company patch.
- Shopify tax lines with actual amount zero do not force a nonzero Odoo tax mapping. The
  focused zero-tax regression and positive-amount missing-mapping regression passed with
  the Odoo/OCB CI at `338c41e7`.
- The duplicate cancellation logic was replaced by OCA's existing `_cancel_sale_order`
  helper in `497c396b`.
- The inspected currency, refund, credit-note, return and total-verification methods
  match the integrated OCA implementation. The additional invoice/payment cutoff belongs
  to SW-OCA-002, not an independent SW-OCA-009 payment override.

**Remaining regression coverage:** The two legacy product-resolution fallbacks still
need dedicated tests for unique SKU, ambiguous SKU and custom line behavior before
production release acceptance. Their present retention is an explicit compatibility
choice, not proof of complete runtime coverage.
