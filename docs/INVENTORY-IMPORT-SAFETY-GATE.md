# Inventory import: independent safety release gate

**Status: NOT APPROVED / do not enable on Switchology production.** **Date:**
2026-11-20. **Source:** OCA/connector-shopify
[PR #67](https://github.com/OCA/connector-shopify/pull/67), merged to **18.0** on
2026-11-19, **not** verified in the 19.0 integration branch.

## Why import needs a separate approval

The currently integrated OCA 19.0
`shopify_connector/models/inventory_sync.py::_handle_inventory_level_update` takes the
webhook's Shopify `available` quantity and compares it to Odoo's inventory. A Shopify
sale can lower `available` by moving a unit into `committed`, without reducing Shopify
`on_hand`. Later validation of the same sale's outgoing picking can reduce Odoo physical
stock a second time.

This is a **plausible, documented double-count failure mode** inherited from upstream
19.0, not proof that Switchology production is currently affected. The OCA 18.0 fix
reads Shopify `on_hand`, distinguishes order reservations from actual stock changes, and
accounts for fulfillment events and their queue ordering. It is **not** a safe text-only
cherry-pick into Odoo 19.0.

### Reproduction target (controlled, mocked Shopify)

| Event                                                     | Shopify available | Shopify committed | Shopify on_hand | Expected Odoo on_hand |
| --------------------------------------------------------- | ----------------: | ----------------: | --------------: | --------------------: |
| Start                                                     |                 8 |                 0 |               8 |                     8 |
| Shopify order reserves one                                |                 7 |                 1 |               8 |                     8 |
| Odoo picking delivered and Shopify fulfilled              |                 7 |                 0 |               7 |                     7 |
| Receipt of 5 at Shopify, if Shopify drives physical stock |                12 |                 0 |              12 |                    12 |

The 19.0 webhook handler must **not** apply the first row-to-row `available` difference
as a physical loss when an Odoo delivery later accounts for the same unit. Test
webhook-before-order, order-before-webhook, late/replayed webhooks, duplicate and
partial fulfillments, cancellations, mapped locations, quant changes, negative
inventory, stock receipts, failed jobs, echo suppression, and concurrent queue
processing. A failure in any of these cases blocks approval.

## Production policy — independent of PR #17

1. `shopify.instance.inventory_import_enabled` must remain **False** unless the
   inventory import gate has explicit approval and corresponding Odoo 19 regression
   tests pass. **Never silently enable** this flag through migration, data default,
   admin UI action, or a cron.
2. Record its **actual** per-instance value from the isolated post-cleanup clone. The
   default is `False`, but that does not prove existing production records are disabled.
   If an actual instance is enabled, escalate the risk for immediate operational review
   — do not change production from this PR.
3. `inventory_export` is an **independent** data-ownership decision. Keep its
   product/variant opt-ins and location policy intact; fixing import must not enable
   additional outbound Shopify inventory mutations.
4. The Odoo 18.0 fix in OCA PR #67 is a design/reference candidate only. Port as a
   focused Odoo 19.0 change, preferably upstream, with tests covering fulfillment
   idempotency and ledger/snapshot behavior. Do not merge an unreviewed, more extensive
   supplier-SKU stock system.
5. Disabled-path regression: `test_inventory_import_remains_disabled_by_default` and
   `test_inventory_webhook_does_not_change_stock_when_import_disabled` in
   `shopify_connector/tests/test_inventory.py` verify defaults and that an inbound
   webhook produces **no stock mutation and no Shopify client call** when the flag is
   disabled.
6. **Separate approval artifact:** inventory source of truth and location mapping,
   field/instance flags, successful 19.0 tests, isolated post-cleanup DB migration,
   before/after stock reconciliation, monitoring, safe rollback and named human release
   approval.

## Operational release criteria

- [ ] Confirm the actual production setting, without editing production.
- [ ] Confirm Shopify vs Odoo physical stock ownership for each mapped location.
- [ ] Decide whether inventory _import_ is needed at all for Switchology; Shopify order
      imports and outbound quantity updates do not require it.
- [ ] Reproduce the documented double-count failure in isolated Odoo 19.
- [ ] Port or design a minimal fix with red-before/green-after regression.
- [ ] Prove idempotency for fulfillment, retries, delayed webhooks and order
      reservations; validate `on_hand` and `available` semantics.
- [ ] Approve a dedicated rollout/rollback independently of PR #17.

This gate **does not block source merge if import is demonstrably disabled** and no
deployment accidentally changes the disabled setting. It **does block enabling inventory
import** or claiming inbound inventory reconciliation is safe.

No production, Shopify, queue, cron, or Odoo submodule change is authorized by this
document.

## Isolated snapshot observation (2026-11-21)

An isolated production-derived database snapshot showed
`inventory_import_enabled=False` on the active instance. Product export,
customer export, and fulfillment export were also disabled. There were
22 opt-in product bindings and 36 opt-in tracked variants, no tested
parent-binding/instance inconsistencies, and one active mapped Shopify
location associated with the internal Odoo `WH/Stock` location.
This observation **does not** prove the live production flags or establish
that inbound inventory import is safe. The independent import gate remains open.
