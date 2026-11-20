"""One-shot resolution of the audited OCA 19.0 three-way merge.

Only run against the exact pinned upstream commit after a real git merge.
This is a temporary integration tool, not a reusable conflict resolver.
"""
import ast
import collections
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFLICT = re.compile(r"^<<<<<<< HEAD\n(.*?)^=======\n(.*?)^>>>>>>> .*?\n", re.M | re.S)
CHOICES = {
    "shopify_connector/__manifest__.py": ["u"],
    "shopify_connector/graphql/order.py": ["u", "u", "o"],
    "shopify_connector/lib/order.py": ["u"],
    "shopify_connector/models/order_binding.py": ["o"],
    "shopify_connector/models/product_binding.py": ["u"],
    "shopify_connector/models/product_sync.py": ["u"],
    "shopify_connector/tests/__init__.py": ["both"],
    "shopify_connector/tests/test_fulfillment_inbound.py": ["o"],
    "shopify_connector/tests/test_product_sync.py": ["import", "both"],
    "shopify_connector/tests/test_fulfillment_sync.py": ["import", "both"],
    "shopify_connector/tests/test_order_sync.py": [
        "imports1", "imports2", "o", "o", "o", "o", "o", "o", "o"
    ],
    "shopify_connector/models/order_sync.py": [
        "o", "u", "o", "o", "o", "o", "o", "aux", "o", "o"
    ],
    "shopify_connector/models/fulfillment_sync.py": [
        "u", "u", "u", "u", "u", "o", "o", "u"
    ],
}


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise AssertionError(f"Expected exactly one occurrence of {old[:90]!r}")
    return source.replace(old, new)


def resolve(path, choices):
    file = ROOT / path
    data = file.read_text()
    found = list(CONFLICT.finditer(data))
    if len(found) != len(choices):
        raise AssertionError(f"{path}: {len(found)} conflict blocks, expected {len(choices)}")
    sequence = iter(choices)

    def resolve_block(match):
        choice = next(sequence)
        ours, upstream = match.group(1), match.group(2)
        if choice == "o":
            return ours
        if choice == "u":
            return upstream
        if choice == "both":
            return ours.rstrip("\n") + "\n\n" + upstream.lstrip("\n")
        if choice == "import":
            if "product_sync" in path:
                return "import psycopg2\n\nfrom odoo.exceptions import UserError, ValidationError\n"
            return (
                "from odoo import fields\n"
                "from odoo.exceptions import AccessError, UserError\n"
                "from odoo.tests.common import TransactionCase, new_test_user\n"
            )
        if choice == "imports1":
            return upstream
        if choice == "imports2":
            return "from odoo.addons.queue_job.exception import RetryableJobError\n" + ours
        if choice == "aux":
            if "self._remove_auxiliary_lines" not in ours or "    def _apply_shipping_carrier" not in upstream:
                raise AssertionError("Unexpected shipping/auxiliary conflict")
            before, after = upstream.split("    def _apply_shipping_carrier", 1)
            marker = "    # SW-OCA-006"
            if marker not in ours:
                raise AssertionError("Missing auxiliary policy marker")
            return (
                ours[:ours.index(marker)]
                + "        self._apply_shipping_carrier(instance, sale_order, order_data)\n\n"
                + ours[ours.index(marker):]
                + "    def _apply_shipping_carrier" + after
            )
        raise AssertionError(f"Unknown selection {choice}")

    result = CONFLICT.sub(resolve_block, data)
    if any(token in result for token in ("<<<<<<< HEAD", ">>>>>>> ")):
        raise AssertionError(f"{path}: conflict markers remain")
    file.write_text(result)


def remove_second_methods(path, classes):
    file = ROOT / path
    lines = file.read_text().splitlines(keepends=True)
    parsed = ast.parse("".join(lines))
    ranges = []
    for cls in parsed.body:
        if not isinstance(cls, ast.ClassDef) or cls.name not in classes:
            continue
        seen = set()
        for node in cls.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in classes[cls.name]:
                continue
            if node.name in seen:
                first = min([node.lineno] + [x.lineno for x in node.decorator_list]) - 1
                ranges.append((first, node.end_lineno))
            seen.add(node.name)
    for start, end in reversed(ranges):
        del lines[start:end]
    file.write_text("".join(lines))


for filename, preferences in CHOICES.items():
    resolve(filename, preferences)

remove_second_methods(
    "shopify_connector/models/order_sync.py",
    {
        "ShopifyInstanceOrderSync": {
            "_job_sync_updated_orders", "_orders_not_imported_yet",
            "_fetch_updated_orders", "_queue_order_imports"
        },
        "ShopifyOrderSync": {"_remove_line"},
    },
)
remove_second_methods(
    "shopify_connector/tests/test_order_sync.py",
    {
        "TestShopifyOrderSync": {
            "_two_line_payload", "_import_confirmed_two_line_order",
            "test_refunded_line_of_confirmed_order_keeps_quantity_zero",
            "test_removed_line_of_confirmed_order_keeps_quantity_zero",
            "test_removed_line_of_quotation_is_deleted",
        }
    },
)

p = ROOT / "shopify_connector/models/order_sync.py"
s = p.read_text()
s = replace_once(
    s,
    "        return self._queue_order_imports(orders, date_from, date_to)\n",
    '        count = self._queue_order_imports(orders)\n'
    '        if self.draft_order_import_enabled:\n'
    '            # SW-OCA-003: queue drafts on bulk import, never from drift.\n'
    '            count += self._queue_draft_orders(date_from, date_to)\n'
    '        return count\n',
)
p.write_text(s)

p = ROOT / "shopify_connector/models/fulfillment_sync.py"
s = p.read_text()
replacements = [
    (
        '    def _queue_missed_fulfillment_pushes(self):\n'
        '        """Queue the push of done deliveries that were never sent to Shopify.',
        '    # SW-OCA-005: no late fulfillment enqueue without explicit export opt-in.\n'
        '    def _queue_missed_fulfillment_pushes(self):\n'
        '        """Queue the push of done deliveries that were never sent to Shopify.',
    ),
    (
        '        if self.env.context.get("skip_shopify_fulfillment_push"):\n'
        '            return\n'
        '        summary = self._fulfillment_push_failed_summary()',
        '        if (\n'
        '            self.env.context.get("skip_shopify_fulfillment_push")\n'
        '            or not self.instance_id.active\n'
        '            or not self.instance_id.fulfillment_export_enabled\n'
        '        ):\n'
        '            return\n'
        '        summary = self._fulfillment_push_failed_summary()',
    ),
    (
        '    def _job_push_picking_fulfillment(self, picking_id):\n'
        '        result = self._push_picking_fulfillment(picking_id)',
        '    # SW-OCA-005: already-queued jobs must also respect export opt-in.\n'
        '    def _job_push_picking_fulfillment(self, picking_id):\n'
        '        if not (self.instance_id.active and self.instance_id.fulfillment_export_enabled):\n'
        '            return False\n'
        '        result = self._push_picking_fulfillment(picking_id)',
    ),
    (
        '    def _job_update_tracking(self):\n'
        '        """Send the current carrier and tracking number of the delivery."""',
        '    # SW-OCA-005: tracking updates are Shopify writes.\n'
        '    def _job_update_tracking(self):\n'
        '        """Send the current carrier and tracking number of the delivery."""',
    ),
    (
        '            self.instance_id.active\n'
        '            and self.shopify_id\n'
        '            and self.origin == "odoo"',
        '            self.instance_id.active\n'
        '            and self.instance_id.fulfillment_export_enabled\n'
        '            and self.shopify_id\n'
        '            and self.origin == "odoo"',
    ),
    (
        '    def _queue_shopify_tracking_update(self):\n'
        '        """Send a carrier or tracking number changed after the push."""',
        '    # SW-OCA-005: only enqueue tracking with explicit export opt-in.\n'
        '    def _queue_shopify_tracking_update(self):\n'
        '        """Send a carrier or tracking number changed after the push."""',
    ),
    (
        '                lambda item: item.shopify_id and item.origin == "odoo"\n',
        '                lambda item: (\n'
        '                    item.shopify_id\n'
        '                    and item.origin == "odoo"\n'
        '                    and item.instance_id.fulfillment_export_enabled\n'
        '                )\n',
    ),
    (
        '            bindings = picking.sale_id.shopify_binding_ids.filtered(\n'
        '                lambda item: (\n'
        '                    not item.is_draft\n'
        '                    and item.state == "synced"\n'
        '                    and item.instance_id.fulfillment_export_enabled\n'
        '                )\n'
        '            )\n'
        '            synced = bindings.filtered(lambda item: item.state == "synced")',
        '            bindings = picking.sale_id.sudo().shopify_binding_ids.filtered(\n'
        '                lambda item: (\n'
        '                    not item.is_draft\n'
        '                    and item.instance_id.fulfillment_export_enabled\n'
        '                )\n'
        '            )\n'
        '            synced = bindings.filtered(lambda item: item.state == "synced")',
    ),
    (
        '    def _action_done(self, *args, **kwargs):\n'
        '        result = super()._action_done(*args, **kwargs)',
        '    # SW-OCA-005: outbound picking completion must be opted in.\n'
        '    def _action_done(self, *args, **kwargs):\n'
        '        result = super()._action_done(*args, **kwargs)',
    ),
    (
        '    def _confirm_external_order(self):\n'
        '        sale_order = self.order_binding_id.odoo_id',
        '    # SW-OCA-002: inbound fulfillment respects historical confirmation cutoff.\n'
        '    def _confirm_external_order(self):\n'
        '        sale_order = self.order_binding_id.odoo_id',
    ),
]
for before, after in replacements:
    s = replace_once(s, before, after)
p.write_text(s)

# Block accidental Python shadowing: duplicate methods otherwise undo guards.
for path in (ROOT / "shopify_connector").rglob("*.py"):
    parsed = ast.parse(path.read_text())
    for cls in parsed.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        definitions = collections.Counter(
            node.name for node in cls.body if isinstance(node, ast.FunctionDef)
        )
        repeated = [name for name, count in definitions.items() if count > 1]
        if repeated:
            raise AssertionError(f"{path}: {cls.name} duplicate methods {repeated}")

print("Merged 13 conflict files; checked AST and duplicate class methods.")
