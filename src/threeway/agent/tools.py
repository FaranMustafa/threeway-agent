"""Tools the agent can call. All are read-only except submit_resolution, which ends the run.

Tool results are JSON so the model sees exact cents, never formatted floats.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from threeway.db import Store
from threeway.matching import received_quantities

SUBMIT = "submit_resolution"


def _tool(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


_STR = {"type": "string"}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool(
        "get_invoice",
        "Fetch a parsed 810 invoice: vendor, PO reference, date, lines (sku, qty, "
        "unit_price_cents) and the stated TDS total.",
        {"invoice_number": _STR},
    ),
    _tool(
        "get_purchase_order",
        "Fetch the parsed 850 purchase order with its ordered lines and agreed unit prices.",
        {"po_number": _STR},
    ),
    _tool(
        "get_ship_notices",
        "Fetch every 856 advance ship notice for a PO. This is the vendor's claim of what "
        "shipped; a line may name the SKU it substitutes for.",
        {"po_number": _STR},
    ),
    _tool(
        "get_receiving_log",
        "Fetch the warehouse receiving log for a PO: what the dock actually counted, with "
        "free-text notes about damage, rejections or substitutions. Treat it as ground truth "
        "for quantity.",
        {"po_number": _STR},
    ),
    _tool(
        "get_contract_price",
        "Look up the contracted unit price for a vendor SKU in effect on a date (YYYY-MM-DD). "
        "Contract amendments can raise prices after the PO was cut.",
        {"vendor_id": _STR, "sku": _STR, "as_of": _STR},
    ),
    _tool(
        "get_vendor_profile",
        "Fetch vendor terms (for example freight terms), price tolerance, and notes from "
        "procurement and AP about this vendor, newest first.",
        {"vendor_id": _STR},
    ),
    _tool(
        "find_related_invoices",
        "List the vendor's other invoices against the same PO, to spot duplicates and "
        "prior billing.",
        {"vendor_id": _STR, "po_number": _STR},
    ),
    _tool(
        SUBMIT,
        "Submit the final resolution. Call exactly once, after investigating. Decisions: "
        "'approve' pays the invoice line total in full; 'approve_adjusted' pays the line total "
        "minus the adjustments; 'reject_duplicate' pays nothing because the invoice repeats an "
        "earlier one; 'escalate' pays nothing and routes to a person when the evidence cannot "
        "settle it. approved_amount_cents must be consistent with the decision. evidence lists "
        "the tool_use ids whose results support the decision.",
        {
            "decision": {
                "type": "string",
                "enum": ["approve", "approve_adjusted", "reject_duplicate", "escalate"],
            },
            "approved_amount_cents": {"type": "integer"},
            "adjustments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sku": _STR,
                        "reason": _STR,
                        "amount_cents": {"type": "integer"},
                    },
                    "required": ["sku", "reason", "amount_cents"],
                    "additionalProperties": False,
                },
            },
            "rationale": _STR,
            "evidence": {"type": "array", "items": _STR},
            "vendor_message": {
                "type": ["string", "null"],
                "description": "Draft note to the vendor when money is withheld, else null.",
            },
        },
    ),
]


class ToolError(Exception):
    """Raised for a bad tool call; the message goes back to the model as is_error."""


class Toolbox:
    def __init__(self, store: Store):
        self.store = store

    def call(self, name: str, args: dict[str, Any]) -> str:
        handler = getattr(self, f"_{name}", None)
        if handler is None or name == SUBMIT:
            raise ToolError(f"unknown tool {name!r}")
        return json.dumps(handler(**args), default=str)

    def _get_invoice(self, invoice_number: str):
        inv = self.store.invoice(invoice_number)
        if inv is None:
            raise ToolError(f"invoice {invoice_number} not found")
        return {**inv.model_dump(), "line_total_cents": inv.line_total_cents}

    def _get_purchase_order(self, po_number: str):
        po = self.store.purchase_order(po_number)
        if po is None:
            raise ToolError(f"purchase order {po_number} not found")
        return po.model_dump()

    def _get_ship_notices(self, po_number: str):
        return [asn.model_dump() for asn in self.store.ship_notices(po_number)]

    def _get_receiving_log(self, po_number: str):
        totals, source = received_quantities(self.store, po_number)
        return {"entries": self.store.receipts(po_number), "totals": totals, "source": source}

    def _get_contract_price(self, vendor_id: str, sku: str, as_of: str):
        try:
            day = date.fromisoformat(as_of)
        except ValueError as exc:
            raise ToolError(f"as_of must be YYYY-MM-DD, got {as_of!r}") from exc
        row = self.store.contract_price(vendor_id, sku, day)
        return row or {"vendor_id": vendor_id, "sku": sku, "contract_price": None}

    def _get_vendor_profile(self, vendor_id: str):
        vendor = self.store.vendor(vendor_id)
        if vendor is None:
            raise ToolError(f"vendor {vendor_id} not found")
        return {**vendor, "notes": self.store.vendor_notes(vendor_id)}

    def _find_related_invoices(self, vendor_id: str, po_number: str):
        return [
            {
                "invoice_number": inv.invoice_number,
                "invoice_date": inv.invoice_date,
                "stated_total_cents": inv.stated_total_cents,
                "lines": [line.model_dump() for line in inv.lines],
            }
            for inv in self.store.invoices(vendor_id)
            if inv.po_number == po_number
        ]
