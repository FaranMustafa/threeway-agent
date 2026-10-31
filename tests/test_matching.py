import pytest

from threeway.matching import match_invoice
from threeway.models import ExceptionKind as K


@pytest.mark.parametrize(
    ("invoice", "kinds", "supported"),
    [
        ("INV-1001", set(), 240000),
        ("INV-1002", {K.PRICE_VARIANCE}, 86000),  # contract amendment supports the new price
        ("INV-1003", {K.PRICE_VARIANCE}, 100000),
        ("INV-1004", {K.QTY_NOT_RECEIVED}, 86000),
        ("INV-1005", {K.RECEIPT_NOTE}, 150000),  # rules can't read the damage note
        ("INV-1006", {K.UNKNOWN_ITEM}, 30000),
        ("INV-1007", {K.QTY_NOT_RECEIVED, K.POSSIBLE_DUPLICATE}, 86000),
        ("INV-1008", {K.UNKNOWN_ITEM, K.RECEIPT_NOTE}, 0),
        ("INV-1009", {K.UOM_MISMATCH}, 0),
        ("INV-1010", {K.UNKNOWN_ITEM}, 0),
    ],
)
def test_scenarios(store, invoice, kinds, supported):
    result = match_invoice(store, store.invoice(invoice))
    assert {e.kind for e in result.exceptions} == kinds
    assert result.supported_amount_cents == supported


def test_only_the_later_invoice_is_a_duplicate(store):
    original = match_invoice(store, store.invoice("INV-1004"))
    assert K.POSSIBLE_DUPLICATE not in {e.kind for e in original.exceptions}


def test_within_tolerance_is_not_a_variance(store):
    # V100 allows 1%: 12.12 on a 12.00 PO line passes.
    inv = store.invoice("INV-1001").model_copy(deep=True)
    inv.lines[0].unit_price_cents = 1212
    inv.stated_total_cents = inv.line_total_cents
    assert match_invoice(store, inv).clean
    inv.lines[0].unit_price_cents = 1213
    inv.stated_total_cents = inv.line_total_cents
    assert {e.kind for e in match_invoice(store, inv).exceptions} == {K.PRICE_VARIANCE}


def test_total_mismatch(store):
    inv = store.invoice("INV-1001").model_copy(update={"stated_total_cents": 1})
    assert K.TOTAL_MISMATCH in {e.kind for e in match_invoice(store, inv).exceptions}
