# Switchology deviations from OCA connector-shopify (Odoo 19)

> **Status: OCA source merged into a DRAFT branch; behavioral/upgrade verification pending.**
> Last reviewed: 2026-11-20.
> Switchology fork baseline: `33a308f9774eba7fcc4bf3634397dbe873f0caec` (`19.0`).
> OCA upstream reference: `5454e91b2f1aa6225026b8cbf6058486607042f8` (`19.0`).
> Merge base: `64bac8e02a18a19f9b80922d89a009dbcc06eb2f`.
> At baseline, fork was 37 commits ahead / 188 behind upstream; 25 files differed.
> Update these references after each upstream integration or meaningful policy change.
> Integration merge commit: `a6293a1974a3db62a74023b5b91930aca8f50155`
> (parents: integration branch `369d90b0` and OCA `5454e91b`).
> This is a **source integration only**, not a deployed or tested schema upgrade.
> Post-merge diff against OCA has to be reviewed after follow-up fixes; do not
> interpret the baseline 25-file inventory as the final difference count.


## Purpose and conventions

The goal is **minimal permanent divergence** from `OCA/connector-shopify:19.0`.
Document each intentionally retained behavioral difference here, and mark its implementation
with a matching short comment: `# SW-OCA-###: reason; see docs/OCA-DEVIATIONS.md`.
Comments identify **policy/behavior boundaries**, not every internal helper or every line.
Never paste this entire table into source files.

Every deviation requires: ID, upstream behavior, Switchology behavior, business reason,
location(s), test evidence, and a disposition (`KEEP`, `UPSTREAM`, `REMOVE`, `REVIEW`).
If a difference becomes identical to upstream, delete the code marker and close or move
the ledger entry to the resolved section. `KEEP` does not mean the implementation
must stay fork-specific forever; revisit available OCA options first.

### Required safety boundaries

Shopify is authoritative for customer sales/payment/invoicing, storefront product content,
shipping and fulfillment. Odoo sale orders exist for internal stock demand and MRP.
A confirmed Odoo order/picking does **not** authorize a Shopify fulfillment.

Any OCA merge must retain historical creation-date and auto-confirm cutoffs,
incomplete-address protection, queue/drift durability, and outbound mutation safeguards.
Verify by **tests** rather than by presence of comments alone.

## Deliberate or provisional deviations

| ID | Status | Upstream baseline vs. Switchology behavior | Why / affected code | Release and test gate |
|---|---|---|---|---|
| **SW-OCA-001** | **KEEP** | Fork rejects creation/retry of old unbound or failed Shopify orders based on **Shopify creation date**, even when an update webhook arrives later; already-synced historical orders can update. OCA's importer has no equivalent fail-closed cutoff at the shared import job. | `models/order_sync.py::_job_import_order`; `models/order_binding.py::order_import_date_from`. Prevent recreation of the 270 removed historical sale orders / 281 bindings after the 2026-11-19 one-off cleanup. | Test webhook/API/bulk/drift replay; missing and invalid creation timestamps; synced older bindings update; no duplicate recreation. Do **not** remove after cleanup. |
| **SW-OCA-002** | **KEEP** | Separate Shopify creation-time auto-confirm cutoff, beyond regular payment eligibility. | `models/order_binding.py::auto_confirm_order_date_from`, `models/order_sync.py::_is_after_confirmation_cutoff` and `_should_confirm_shopify_order`; also inbound order confirmation paths. Protect old technical demand documents from unexpected stock procurement/confirmations. | Verify drafts, paid/partially-paid updates, inbound fulfillment, retained historical bindings, and existing instance timestamp on a restored DB. |
| **SW-OCA-003** | **REVIEW** | Fork maintains a durable `order_drift_pending` ledger and reconciles updates not yet acknowledged after the cursor advances. Upstream reconciliation has changed independently. | `models/order_binding.py::order_drift_pending`, `models/order_sync.py::_job_sync_updated_orders` and success/acknowledgment logic. | Compare OCA's latest reliability guarantees and failure semantics before deciding to keep the fork override; test job failure, replay, dropped queue work, race, and a pending update such as #1345. |
| **SW-OCA-004** | **KEEP** | Product, price, image and metadata mutations are fail-closed when instance `product_export_enabled=False`, including enqueue, manual wizard and execution of old jobs. Independent of inventory export. OCA reference has no equivalent global flag. | `models/field_mapping.py`, `models/product_sync.py`, `wizards/product_export.py`, `views/shopify_instance_views.xml`; added by fork PR #13. | Test both queued and manually invoked mutations, multi-instance flags, and the explicit enabled case. New field needs isolated DB module-upgrade test; confirm existing instances initialize it false. |
| **SW-OCA-005** | **KEEP** | Fulfillment mutations are fail-closed when `fulfillment_export_enabled=False`: outgoing picking enqueue/job, manual hold release/cancel and queued executions. New instances default false. OCA reference lacks this switch and includes **additional** outbound paths. | `models/fulfillment_binding.py`, `models/fulfillment_sync.py`; fork PR #13. | During OCA integration explicitly guard **`_push_picking_fulfillment`, `_queue_missed_fulfillment_pushes`, `_queue_shopify_tracking_update`, `_job_update_tracking`**, and all external mutation entry points, at enqueue **and** job execution. Also block `models/operability.py::action_retry_sync` / `_shopify_enqueue_fresh_retry` before resetting failed outgoing fulfillment bindings; keep their original error when exports are disabled. Test per-instance and already queued jobs. |
| **SW-OCA-006** | **REVIEW / likely KEEP until upstream equivalent** | Fork preserves duty, shipping and subtotal-adjustment auxiliary lines on confirmed orders by setting quantity/amount to zero rather than unlinking. Base OCA retained removed ordinary product lines; exact auxiliary-line equivalence needs reconciliation. | `models/order_sync.py::_remove_auxiliary_lines` and auxiliary helpers; fork PRs #14 and #15. | Confirmed Odoo 19 orders, refunds, omitted shipping/duties, duplicate lines, reactivation, identity and idempotency; do not introduce deletions on confirmed orders. |
| **SW-OCA-007** | **REVIEW** | Fork protects existing shipping/billing contacts when Shopify returns country-only or incomplete order-specific addresses; newer orders can include fully approved protected customer data. | `models/order_sync.py::_build_order` partner selection; fork PRs #8/#11, order normalization. | Confirm semantics against OCA's new address logic. Test both incomplete historical imports and complete newer payloads; never synthesize addresses or confuse customer defaults with order-specific addresses. |
| **SW-OCA-008** | **UPSTREAM merged; test pending** | Fork PR #16 ported much of upstream inbound fulfillment validation, including partial/multi-step/kit handling and carrier-booking suppression. | `models/fulfillment_sync.py`, `graphql/fulfillment.py`, `tests/test_fulfillment_inbound.py`. | Prefer upstream implementation after semantic merge; preserve independent `fulfillment_auto_validate_webhook` and `fulfillment_validate_unreserved` settings and SW-OCA-002/005. No duplicate implementation. |
| **SW-OCA-009** | **REVIEW** | Other altered order normalization, currency/tax/rounding, SKU and refund logic is not yet classified line-by-line. | `graphql/order.py`, `lib/order.py`, `models/order_sync.py`, tests. | Trace each remaining diff against upstream and tests. Prefer OCA's newer fixes; keep only a documented demonstrated behavior. No blanket 'keep all fork fixes'. |
| **SW-OCA-010** | **Source integrated; DB upgrade gate OPEN** | OCA `19.0.1.7.0` removes `website_sale` from the main connector dependencies and adds `shopify_connector_website_sale` plus migration `19.0.1.1.0/pre-migration.py`. Fork is still `19.0.1.0.3`, with media bindings in the base module. | Module manifest, product-image models/controllers, migration and glue module. This is a **schema/module migration**, not a mere source diff. | Isolated post-cleanup restore; verify module available and pre-migration succeeds; preserve product-image bindings. OCA migration raises if image bindings exist and glue module is unavailable. Never trial directly on production. |
| **SW-OCA-011** | **REVIEW / INVENTORY IMPORT GATE OPEN** | Inventory export controls use independent product+variant opt-ins and location mapping. OCA 19.0 inventory **import** still applies Shopify `available` directly to Odoo stock, potentially double-counting a sale once from inventory webhook and again upon delivery validation. The Odoo 18 fix from [OCA PR #67](https://github.com/OCA/connector-shopify/pull/67) is **not integrated into 19.0**. | `models/inventory_sync.py::_handle_inventory_level_update`, `models/inventory_binding.py::inventory_import_enabled`, `tests/test_inventory.py`, `docs/INVENTORY-IMPORT-SAFETY-GATE.md`. | Keep `inventory_import_enabled=False` (default) unless separately approved; verify actual production flag and isolated upgrade preservation. Field `copy=False`, explicit `action_duplicate_configuration` default `False`, and server-side onboarding refusal for `bidirectional` prevent inherited/normal-wizard activation; tests in `test_instance.py`, `test_operability.py`, and `test_inventory.py`. Negative tests assert disabled webhook makes no stock/API writes. Active-import double-count fix and tests remain a **separate release gate**, not authorized by PR #17. Outbound inventory export controls remain independent. |

These entries classify **known functional deviations and migration risks**, not all 25 baseline changed
files. Differences in tests, translations, package versions and display metadata still require
normal merge review; never assume undocumented diffs are approved.

## Source integration outcome (2026-11-20)

- Real two-parent merge commit `a6293a1974a3db62a74023b5b91930aca8f50155`.
  OCA `5454e91b` is an ancestor of the integration branch; the 188 previous
  missing upstream commits are now in its history.
- An actual `git merge --no-commit` reported **13 textual conflict files**
  (41 conflict regions). The remaining overlapping paths merged automatically.
  The decision log is `docs/OCA-MERGE-REVIEW.md`.
- The OCA media glue module `shopify_connector_website_sale` and the upstream
  pre-migration were added to source. No actual database upgrade or migration
  has been performed.
- `SW-OCA-004` and `SW-OCA-005` are retained as explicit outbound
  write safeguards. Newly introduced OCA missed-fulfillment and tracking
  mutation paths are guarded for disabled exports; regression verification
  is still required.
- `SW-OCA-001`, `SW-OCA-002`, `SW-OCA-003`, `SW-OCA-006`, and
  `SW-OCA-007` have provisional merged implementations; their release
  behavior is **not yet accepted**.
- The base fork's `.github/workflows/` definitions are intentionally retained
  because the GitHub Actions app token cannot update workflow files. The
  OCA workflow improvements remain a **separate permissions/review task**.
- Merge-candidate Python syntax and duplicate-class-method checks passed
  in the offline integration rehearsal. This does not validate Odoo behavior.
  GitHub CI and isolated Odoo database upgrade remain independent release gates.
- No Odoo main/submodule production pin or live Shopify configuration changed.

## Baseline changed-file inventory (fork relative to OCA 19.0)

`AGENTS.md`; `shopify_connector/__manifest__.py`;
`shopify_connector/data/ir_cron.xml`;
`shopify_connector/graphql/{fulfillment,inventory,order}.py`;
`shopify_connector/lib/order.py`;
`shopify_connector/models/{field_mapping,fulfillment_binding,fulfillment_sync,inventory_sync,order_binding,order_sync,product_binding,product_sync}.py`;
`shopify_connector/tests/{__init__,test_fulfillment_inbound,test_fulfillment_sync,test_order_sync,test_product_sync}.py`;
`shopify_connector/views/{shopify_fulfillment_views,shopify_instance_views,shopify_order_views}.xml`;
`shopify_connector/wizards/product_export.py`;
`shopify_connector_pos/tests/test_pos.py`.

Review every changed file after merge and update the table above for each **remaining behavior**.

## Integration procedure / evidence

1. **Merge, don't overwrite or force-push:** integrate pinned OCA `19.0` into this dedicated fork branch with semantic conflict review.
2. Record upstream SHA, fork parent SHA, merge commit, diff inventory, and any newly introduced Shopify mutations.
3. For each retained `SW-OCA-###`, map to the smallest implementation location and add the matching source comment; avoid comments in untouched OCA paths.
4. Reject regressions in historical import cutoff, auto-confirm cutoff, drift durability, order lines, incomplete addresses, invoicing policy, inventory opt-in, and Shopify write guards.
5. Run pre-commit and Odoo/OCB integration tests **including both allowed and blocked paths**. Tests should assert external client methods/mutations are not called with export disabled.
6. Test OCA schema migration with isolated, neutralized **post-cleanup** DB + filestore, no live Shopify access, and controlled module upgrade. Record counts/invariants, and don't infer deployment from green CI.
7. Only then open/review an integration PR against fork `19.0`. Production Odoo is separately pinned via `wolke6000/odoo-switchology`; changing this branch alone changes neither production nor Odoo's gitlink.

### Review gate for each future fork-only deviation

Add a ledger row with: **ID → exact entry points → upstream alternative / commit → business constraint → tests → migration effects → decision and reviewer**. The PR must state why the deviation cannot be expressed with upstream configuration or contributed upstream. A new policy without this entry and nearby code marker is incomplete.

## Resolved/upstreamed items

_Upstream code was integrated (not yet verified by Odoo tests). Move individual IDs here only after behavior and upgrade tests pass; do not infer resolved status from a successful Git merge._
