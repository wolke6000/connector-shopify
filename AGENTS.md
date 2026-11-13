# Agent instructions — Switchology connector-shopify fork

## Scope and authority

- This is `wolke6000/connector-shopify`, a maintained fork of `OCA/connector-shopify`,
  for Odoo 19.0 on branch `19.0`.
- Read this file and any more-specific `AGENTS.md` before editing. Check target branch,
  HEAD, working tree, and relevant upstream changes first.
- Follow existing tests, lint rules, OCA coding conventions, and current OCA
  contribution and AI-use policies. This file does not override them.
- Never expose credentials, access tokens, customer personal data, or production
  database extracts.

## Switchology system responsibilities

- Shopify owns customer-facing sales orders, order processing, payment, invoicing,
  shipping, and fulfillment.
- Odoo is used for purchasing, internal stock management, and manufacturing, not
  customer sales order processing or fulfillment.
- Synced Odoo `sale.order` records are technical demand documents that support internal
  stock reservation and manufacturing. Their confirmation does not transfer sales or
  fulfillment authority from Shopify to Odoo.
- The Shopify app is now approved to access protected customer information,
  including full shipping and billing addresses. PR #11 restored the upstream
  GraphQL order address fields on this fork's 19.0 branch. A merged change is
  not a deployment: verify the pinned Odoo submodule and runtime API responses
  before relying on complete address data.
- Historical imported order payloads may contain only country-level fields.
  Do not invent or infer missing addresses, treat absent fields as changes,
  mistake a current customer default address for an order-specific historical
  address, or use Odoo as the source of customer shipping information.

## Branches and upstream

- Never commit directly to shared `19.0`; use focused branches and reviewed PRs.
- Compare with `OCA/connector-shopify:19.0` before introducing or retaining fork-only
  fixes. Prefer equivalent upstream solutions.
- Merge upstream into a dedicated integration branch. Never rewrite or force-push shared
  `19.0` history; resolve conflicts semantically.
- Offer generally useful fixes to OCA in small, independently tested contributions. Do
  not submit Switchology-specific operational policy upstream by default.
- For OCA contributions, follow current PR and AI-disclosure rules. A human must
  understand and take responsibility for every submitted change.

## Safety-critical regression requirements

These are Switchology requirements, not assumptions for every connector installation.

- Preserve the ability to disable automatic Odoo customer invoicing and payment posting.
  Shopify is authoritative for these operations.
- Preserve creation-date import cutoffs and independent auto-confirm cutoffs. Already
  synchronized historical orders may receive permitted updates.
- Preserve idempotency, concurrency safety, durable drift reconciliation and retries,
  cancellation, and refund behavior.
- Keep shop-money and presentment-money amounts and tax calculations consistent.
- Preserve existing Odoo contacts when an order payload still contains only
  country-level or otherwise incomplete addresses; do not overwrite them
  with placeholders. Verify full-address reimports against legacy bindings
  before production deployment.
- Preserve switches for disabling Shopify fulfillment writes, automatic delivery
  validation, and inventory exports. Never silently enable outbound Shopify operations.
- Keep webhook HMAC verification, event deduplication, and safe queue dispatch intact.

## Validation and release

- Add regression tests for behavioral changes, ideally failing without the fix.
- Run applicable pre-commit, Odoo, OCB, and dependency checks; report what was not
  tested.
- When affected, test Shopify writes, historical cutoffs, order changes, cancellations,
  currencies, refunds, fulfillment, and queue recovery.
- CI success or merging this fork does not authorize deployment.
  `wolke6000/odoo-switchology` pins the connector as a Git submodule at an exact commit.
- Never modify production services, data, credentials, Shopify state, or jobs without
  explicit authorization.
