"""Deterministic guardrails around the agent's resolution.

Two layers:
1. validate(): hard rules. A violation goes back to the agent as a tool error so it can fix
   its own arithmetic or cite real evidence. It never reaches a person.
2. route(): decides whether a valid resolution may post automatically or needs a human.
   The agent may recommend paying more than the rules can prove (a substitute part, say),
   but only a person can approve that.
"""

from __future__ import annotations

from dataclasses import dataclass

from threeway.matching import MatchResult
from threeway.models import CaseStatus, Decision, ExceptionKind, Invoice, Resolution, fmt

AUTO_APPROVE_LIMIT_CENTS = 25_000_00


@dataclass(frozen=True)
class Route:
    status: CaseStatus
    reasons: list[str]


def validate(
    resolution: Resolution, invoice: Invoice, match: MatchResult, tool_ids: set[str]
) -> list[str]:
    errors: list[str] = []
    line_total = invoice.line_total_cents
    deducted = sum(a.amount_cents for a in resolution.adjustments)

    match resolution.decision:
        case Decision.APPROVE:
            if resolution.approved_amount_cents != line_total:
                errors.append(
                    f"'approve' must pay the line total {line_total} cents, "
                    f"got {resolution.approved_amount_cents}"
                )
            if resolution.adjustments:
                errors.append("'approve' cannot carry adjustments; use 'approve_adjusted'")
        case Decision.APPROVE_ADJUSTED:
            if not resolution.adjustments:
                errors.append("'approve_adjusted' needs at least one adjustment")
            if resolution.approved_amount_cents != line_total - deducted:
                errors.append(
                    f"approved_amount_cents must equal line total {line_total} minus "
                    f"adjustments {deducted} = {line_total - deducted}, "
                    f"got {resolution.approved_amount_cents}"
                )
        case Decision.REJECT_DUPLICATE | Decision.ESCALATE:
            if resolution.approved_amount_cents != 0:
                errors.append(f"'{resolution.decision}' must have approved_amount_cents = 0")

    if resolution.approved_amount_cents > line_total:
        errors.append("cannot approve more than the invoice line total")

    if resolution.decision != Decision.ESCALATE:
        if not resolution.evidence:
            errors.append("cite at least one tool_use id in evidence")
        unknown = [e for e in resolution.evidence if e not in tool_ids]
        if unknown:
            errors.append(f"evidence cites tool_use ids that were never called: {unknown}")

    invoice_skus = {line.sku for line in invoice.lines}
    stray = [a.sku for a in resolution.adjustments if a.sku not in invoice_skus]
    if stray:
        errors.append(f"adjustments reference SKUs not on the invoice: {stray}")

    if resolution.decision == Decision.REJECT_DUPLICATE and not any(
        e.kind == ExceptionKind.POSSIBLE_DUPLICATE for e in match.exceptions
    ):
        errors.append("no duplicate was detected for this invoice; escalate instead")
    return errors


def route(resolution: Resolution, match: MatchResult) -> Route:
    if resolution.decision == Decision.ESCALATE:
        return Route(CaseStatus.ESCALATED, ["agent escalated: " + resolution.rationale[:200]])

    reasons: list[str] = []
    if resolution.approved_amount_cents > match.supported_amount_cents:
        reasons.append(
            f"approval {fmt(resolution.approved_amount_cents)} exceeds the rule-verified "
            f"amount {fmt(match.supported_amount_cents)}"
        )
    if resolution.approved_amount_cents > AUTO_APPROVE_LIMIT_CENTS:
        reasons.append(f"above auto-approve limit {fmt(AUTO_APPROVE_LIMIT_CENTS)}")
    if reasons:
        return Route(CaseStatus.AWAITING_HUMAN, reasons)
    return Route(CaseStatus.AUTO_RESOLVED, ["within rule-verified amount and limits"])
