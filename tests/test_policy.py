from threeway.agent.policy import route, validate
from threeway.matching import match_invoice
from threeway.models import CaseStatus, Resolution


def res(**kw) -> Resolution:
    base = {"decision": "approve", "approved_amount_cents": 0, "rationale": "r", "evidence": ["t1"]}
    return Resolution.model_validate(base | kw)


def check(store, invoice, resolution, ids=frozenset({"t1"})):
    inv = store.invoice(invoice)
    return validate(resolution, inv, match_invoice(store, inv), set(ids))


def test_approve_must_pay_line_total(store):
    assert check(store, "INV-1002", res(approved_amount_cents=86000)) == []
    assert check(store, "INV-1002", res(approved_amount_cents=80000))


def test_adjusted_arithmetic(store):
    adj = [{"sku": "B-400", "reason": "short", "amount_cents": 9000}]
    ok = res(decision="approve_adjusted", approved_amount_cents=86000, adjustments=adj)
    assert check(store, "INV-1004", ok) == []
    bad = res(decision="approve_adjusted", approved_amount_cents=85000, adjustments=adj)
    assert any("must equal" in e for e in check(store, "INV-1004", bad))


def test_evidence_must_be_real_tool_calls(store):
    errors = check(store, "INV-1002", res(approved_amount_cents=86000, evidence=["made_up"]))
    assert any("never called" in e for e in errors)


def test_adjustment_sku_must_be_on_invoice(store):
    adj = [{"sku": "NOPE", "reason": "x", "amount_cents": 100}]
    r = res(decision="approve_adjusted", approved_amount_cents=85900 + 0, adjustments=adj)
    assert any("not on the invoice" in e for e in check(store, "INV-1004", r))


def test_duplicate_requires_detected_duplicate(store):
    reject = res(decision="reject_duplicate")
    assert check(store, "INV-1007", reject) == []
    assert check(store, "INV-1004", reject)


def test_route_paying_above_rule_verified_needs_a_person(store):
    match = match_invoice(store, store.invoice("INV-1008"))
    assert route(res(approved_amount_cents=54000), match).status == CaseStatus.AWAITING_HUMAN


def test_route_within_verified_amount_posts(store):
    match = match_invoice(store, store.invoice("INV-1002"))
    assert route(res(approved_amount_cents=86000), match).status == CaseStatus.AUTO_RESOLVED


def test_route_escalation(store):
    match = match_invoice(store, store.invoice("INV-1010"))
    assert route(res(decision="escalate"), match).status == CaseStatus.ESCALATED
