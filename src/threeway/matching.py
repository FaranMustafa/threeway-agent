"""Deterministic three-way match: invoice (810) vs purchase order (850) vs what arrived
(receiving log, falling back to the 856 ship notice).

This layer finds exceptions and computes the amount the rules alone can justify. It never
decides how to resolve an exception; that judgment is the agent's job.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from threeway.db import Store
from threeway.models import ExceptionKind, Invoice, MatchException, fmt


@dataclass
class MatchResult:
    invoice_number: str
    exceptions: list[MatchException] = field(default_factory=list)
    # The most the rules can prove is owed: min(invoiced, ordered, received) quantity at
    # min(invoiced, max(PO, contract)) price, for SKUs that are on the PO.
    supported_amount_cents: int = 0

    @property
    def clean(self) -> bool:
        return not self.exceptions


def received_quantities(store: Store, po_number: str) -> tuple[dict[str, int], str]:
    """Quantities that physically arrived. The dock's receiving log wins over the vendor's
    own ship notice because the ASN is only the vendor's claim."""
    receipts = store.receipts(po_number)
    totals: dict[str, int] = defaultdict(int)
    if receipts:
        for r in receipts:
            totals[r["sku"]] += r["qty_received"]
        return dict(totals), "receiving_log"
    for asn in store.ship_notices(po_number):
        for line in asn.lines:
            totals[line.sku] += line.qty_shipped
    return dict(totals), "ship_notice"


def match_invoice(store: Store, invoice: Invoice) -> MatchResult:
    result = MatchResult(invoice.invoice_number)
    po = store.purchase_order(invoice.po_number)
    if po is None:
        result.exceptions.append(
            MatchException(
                kind=ExceptionKind.UNKNOWN_ITEM,
                detail=f"PO {invoice.po_number} not found",
                amount_at_risk_cents=invoice.line_total_cents,
            )
        )
        return result

    vendor = store.vendor(invoice.vendor_id) or {"price_tolerance_bps": 0}
    tolerance_bps = vendor["price_tolerance_bps"]
    po_lines = {line.sku: line for line in po.lines}
    received, source = received_quantities(store, po.po_number)

    if invoice.stated_total_cents != invoice.line_total_cents:
        result.exceptions.append(
            MatchException(
                kind=ExceptionKind.TOTAL_MISMATCH,
                detail=(
                    f"TDS total {fmt(invoice.stated_total_cents)} != sum of lines "
                    f"{fmt(invoice.line_total_cents)}"
                ),
                amount_at_risk_cents=abs(invoice.stated_total_cents - invoice.line_total_cents),
            )
        )

    # Free-text dock notes (damage, substitutions) can change what is owed, but rules
    # cannot read them, so any note on an invoiced SKU is surfaced for review.
    noted = {r["sku"]: r["note"] for r in store.receipts(po.po_number) if r["note"].strip()}
    for line in invoice.lines:
        if line.sku in noted:
            result.exceptions.append(
                MatchException(
                    kind=ExceptionKind.RECEIPT_NOTE,
                    sku=line.sku,
                    detail=f"receiving note: {noted[line.sku]}",
                    amount_at_risk_cents=line.extended_cents,
                )
            )

    for line in invoice.lines:
        po_line = po_lines.get(line.sku)
        if po_line is None:
            result.exceptions.append(
                MatchException(
                    kind=ExceptionKind.UNKNOWN_ITEM,
                    sku=line.sku,
                    detail=f"{line.sku} is not on PO {po.po_number}",
                    amount_at_risk_cents=line.extended_cents,
                )
            )
            continue

        if line.uom != po_line.uom:
            # Quantities and prices are not comparable without a pack-size conversion,
            # which the rules do not know. Nothing on this line is rule-verified.
            result.exceptions.append(
                MatchException(
                    kind=ExceptionKind.UOM_MISMATCH,
                    sku=line.sku,
                    detail=(
                        f"invoiced {line.qty} {line.uom} @ {fmt(line.unit_price_cents)} vs PO "
                        f"{po_line.qty} {po_line.uom} @ {fmt(po_line.unit_price_cents)}"
                    ),
                    amount_at_risk_cents=line.extended_cents,
                )
            )
            continue

        contract = store.contract_price(invoice.vendor_id, line.sku, invoice.invoice_date)
        reference_price = max(
            po_line.unit_price_cents, contract["unit_price_cents"] if contract else 0
        )
        allowed_price = po_line.unit_price_cents * (10_000 + tolerance_bps) // 10_000
        if line.unit_price_cents > allowed_price:
            result.exceptions.append(
                MatchException(
                    kind=ExceptionKind.PRICE_VARIANCE,
                    sku=line.sku,
                    detail=(
                        f"invoiced {fmt(line.unit_price_cents)} vs PO "
                        f"{fmt(po_line.unit_price_cents)} (tolerance {tolerance_bps / 100:.2f}%)"
                    ),
                    amount_at_risk_cents=line.qty
                    * (line.unit_price_cents - po_line.unit_price_cents),
                )
            )

        if line.qty > po_line.qty:
            result.exceptions.append(
                MatchException(
                    kind=ExceptionKind.QTY_OVER_ORDERED,
                    sku=line.sku,
                    detail=f"invoiced {line.qty} but PO ordered {po_line.qty}",
                    amount_at_risk_cents=(line.qty - po_line.qty) * line.unit_price_cents,
                )
            )

        got = received.get(line.sku, 0)
        if line.qty > got:
            result.exceptions.append(
                MatchException(
                    kind=ExceptionKind.QTY_NOT_RECEIVED,
                    sku=line.sku,
                    detail=f"invoiced {line.qty} but {source} shows {got}",
                    amount_at_risk_cents=(line.qty - got) * line.unit_price_cents,
                )
            )

        qty = min(line.qty, po_line.qty, got)
        result.supported_amount_cents += qty * min(line.unit_price_cents, reference_price)

    # Only the later of two look-alike invoices is flagged, so the original still pays.
    for other in store.invoices(invoice.vendor_id):
        earlier = (other.invoice_date, other.invoice_number) < (
            invoice.invoice_date,
            invoice.invoice_number,
        )
        if (
            earlier
            and other.po_number == invoice.po_number
            and other.stated_total_cents == invoice.stated_total_cents
        ):
            result.exceptions.append(
                MatchException(
                    kind=ExceptionKind.POSSIBLE_DUPLICATE,
                    detail=(
                        f"{other.invoice_number} has the same PO and total "
                        f"{fmt(other.stated_total_cents)}"
                    ),
                    amount_at_risk_cents=invoice.stated_total_cents,
                )
            )

    return result
