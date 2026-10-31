"""Domain models. All money is integer cents; conversion happens only at the edges."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field


def to_cents(value: str) -> int:
    """Parse a decimal string like '12.5' into cents without float rounding."""
    whole, _, frac = value.strip().partition(".")
    sign = -1 if whole.startswith("-") else 1
    frac = (frac + "00")[:2]
    return sign * (abs(int(whole or "0")) * 100 + int(frac))


def fmt(cents: int) -> str:
    return f"${cents / 100:,.2f}"


class Line(BaseModel):
    line_no: int
    sku: str
    qty: int
    uom: str = "EA"
    unit_price_cents: int = 0

    @property
    def extended_cents(self) -> int:
        return self.qty * self.unit_price_cents


class PurchaseOrder(BaseModel):
    po_number: str
    vendor_id: str
    order_date: date
    lines: list[Line]


class ShipmentLine(BaseModel):
    sku: str
    qty_shipped: int
    substitute_for: str | None = None


class ShipNotice(BaseModel):
    asn_id: str
    po_number: str
    ship_date: date
    lines: list[ShipmentLine]


class Invoice(BaseModel):
    invoice_number: str
    vendor_id: str
    po_number: str
    invoice_date: date
    lines: list[Line]
    stated_total_cents: int

    @property
    def line_total_cents(self) -> int:
        return sum(line.extended_cents for line in self.lines)


class ExceptionKind(StrEnum):
    PRICE_VARIANCE = "price_variance"
    QTY_NOT_RECEIVED = "qty_not_received"
    QTY_OVER_ORDERED = "qty_over_ordered"
    UNKNOWN_ITEM = "unknown_item"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    TOTAL_MISMATCH = "total_mismatch"
    UOM_MISMATCH = "uom_mismatch"
    RECEIPT_NOTE = "receipt_note"


class MatchException(BaseModel):
    kind: ExceptionKind
    sku: str | None = None
    detail: str
    amount_at_risk_cents: int = 0


class Decision(StrEnum):
    APPROVE = "approve"
    APPROVE_ADJUSTED = "approve_adjusted"
    REJECT_DUPLICATE = "reject_duplicate"
    ESCALATE = "escalate"


class Adjustment(BaseModel):
    sku: str
    reason: str
    amount_cents: int = Field(ge=0, description="Amount deducted from the invoice, in cents")


class Resolution(BaseModel):
    decision: Decision
    approved_amount_cents: int = Field(ge=0)
    adjustments: list[Adjustment] = []
    rationale: str
    evidence: list[str] = Field(
        default_factory=list, description="tool_use ids whose results support the decision"
    )
    vendor_message: str | None = None


class CaseStatus(StrEnum):
    AUTO_RESOLVED = "auto_resolved"
    AWAITING_HUMAN = "awaiting_human"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"
