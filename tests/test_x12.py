import pytest

from threeway.edi.builder import build
from threeway.edi.x12 import X12Error, parse, tokenize
from threeway.models import Invoice, PurchaseOrder, ShipNotice, to_cents
from threeway.seed import DATA_DIR


def test_to_cents_is_exact():
    assert to_cents("12.5") == 1250
    assert to_cents("0.07") == 7
    assert to_cents("100") == 10000
    assert to_cents("-3.10") == -310


def test_sample_files_parse():
    docs = [d for p in sorted((DATA_DIR / "edi").glob("*.edi")) for d in parse(p.read_text())]
    assert sum(isinstance(d, PurchaseOrder) for d in docs) == 8
    assert sum(isinstance(d, ShipNotice) for d in docs) == 8
    assert sum(isinstance(d, Invoice) for d in docs) == 10


def test_810_fields():
    raw = build(
        "810",
        [["BIG*20260910*INV-9**PO-1", "N1*VN*X*92*V1", "IT1*1*3*CS*1.25**VP*A-1", "TDS*375"]],
    )
    (inv,) = parse(raw)
    assert inv.invoice_number == "INV-9" and inv.po_number == "PO-1" and inv.vendor_id == "V1"
    assert inv.lines[0].uom == "CS" and inv.lines[0].unit_price_cents == 125
    assert inv.stated_total_cents == inv.line_total_cents == 375


def test_substitute_line_in_856():
    raw = build("856", [["BSN*00*A1*20260901*1200", "PRF*PO-1", "LIN**VP*B*SU*A", "SN1**4*EA"]])
    (asn,) = parse(raw)
    assert asn.lines[0].sku == "B" and asn.lines[0].substitute_for == "A"


def test_delimiters_come_from_isa():
    raw = build("850", [["BEG*00*SA*PO-7**20260101", "N1*VN*X*92*V1", "PO1*1*2*EA*3.00**VP*Z"]])
    custom = raw.replace("*", "|").replace("~", "\n")
    # The ISA's own repetition/sub-element character ('>') stays put; only delimiters change.
    (po,) = parse(custom)
    assert po.po_number == "PO-7" and po.lines[0].sku == "Z"
    assert tokenize(custom)[0].tag == "ISA"


def test_segment_count_is_checked():
    raw = build("850", [["BEG*00*SA*PO-7**20260101", "N1*VN*X*92*V1"]]).replace("SE*4*", "SE*9*")
    with pytest.raises(X12Error, match="SE01"):
        parse(raw)


def test_unsupported_transaction():
    raw = build("850", [["BEG*00*SA*PO-7**20260101"]]).replace("ST*850", "ST*997")
    with pytest.raises(X12Error, match="unsupported"):
        parse(raw)


def test_rejects_non_x12():
    with pytest.raises(X12Error):
        parse("hello")
