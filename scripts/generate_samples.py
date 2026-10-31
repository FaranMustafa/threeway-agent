"""Regenerate data/edi/*.edi from the scenario table below.

Run: uv run python scripts/generate_samples.py
"""

from pathlib import Path

from threeway.edi.builder import build

OUT = Path(__file__).resolve().parent.parent / "data" / "edi"

VENDORS = {"V100": "ACME FASTENERS", "V200": "NORTHWIND INDUSTRIAL"}


def n1(vendor: str) -> str:
    return f"N1*VN*{VENDORS[vendor]}*92*{vendor}"


def po(number: str, vendor: str, date: str, lines: list[tuple]) -> list[str]:
    body = [f"BEG*00*SA*{number}**{date}", n1(vendor)]
    for i, (sku, qty, price, *uom) in enumerate(lines, start=1):
        body.append(f"PO1*{i}*{qty}*{uom[0] if uom else 'EA'}*{price}**VP*{sku}")
    body.append(f"CTT*{len(lines)}")
    return body


def asn(asn_id: str, po_number: str, date: str, lines: list[tuple]) -> list[str]:
    body = [f"BSN*00*{asn_id}*{date}*1200", "HL*1**S", f"PRF*{po_number}"]
    for sku, qty, *sub in lines:
        lin = f"LIN**VP*{sku}" + (f"*SU*{sub[0]}" if sub else "")
        body += [lin, f"SN1**{qty}*EA"]
    return body


def invoice(number, vendor, po_number, date, lines, total_cents=None) -> list[str]:
    body = [f"BIG*{date}*{number}**{po_number}", n1(vendor)]
    total = 0
    for i, (sku, qty, price, *uom) in enumerate(lines, start=1):
        body.append(f"IT1*{i}*{qty}*{uom[0] if uom else 'EA'}*{price}**VP*{sku}")
        total += qty * round(float(price) * 100)
    body.append(f"TDS*{total_cents if total_cents is not None else total}")
    return body


PURCHASE_ORDERS = [
    po("PO-5001", "V100", "20260801", [("W-100", 200, "12.00")]),
    po("PO-5002", "V100", "20260820", [("W-110", 100, "8.00")]),
    po("PO-5003", "V200", "20260822", [("G-300", 40, "25.00")]),
    po("PO-5004", "V200", "20260825", [("B-400", 100, "4.50"), ("H-410", 50, "10.00")]),
    po("PO-5005", "V100", "20260826", [("P-500", 50, "30.00")]),
    po("PO-5006", "V100", "20260828", [("C-600", 20, "15.00")]),
    po("PO-5008", "V200", "20260902", [("W-200", 30, "18.00")]),
    po("PO-5009", "V200", "20260903", [("T-900", 10, "120.00", "CS")]),
]

SHIP_NOTICES = [
    asn("ASN-7001", "PO-5001", "20260805", [("W-100", 200)]),
    asn("ASN-7002", "PO-5002", "20260905", [("W-110", 100)]),
    asn("ASN-7003", "PO-5003", "20260901", [("G-300", 40)]),
    asn("ASN-7004", "PO-5004", "20260905", [("B-400", 100), ("H-410", 50)]),
    asn("ASN-7005", "PO-5005", "20260904", [("P-500", 50)]),
    asn("ASN-7006", "PO-5006", "20260906", [("C-600", 20)]),
    asn("ASN-7008", "PO-5008", "20260910", [("W-200B", 30, "W-200")]),
    asn("ASN-7009", "PO-5009", "20260911", [("T-900", 10)]),
]

INVOICES = [
    invoice("INV-1001", "V100", "PO-5001", "20260808", [("W-100", 200, "12.00")]),
    invoice("INV-1002", "V100", "PO-5002", "20260910", [("W-110", 100, "8.60")]),
    invoice("INV-1003", "V200", "PO-5003", "20260905", [("G-300", 40, "26.50")]),
    invoice(
        "INV-1004", "V200", "PO-5004", "20260915", [("B-400", 100, "4.50"), ("H-410", 50, "10.00")]
    ),
    invoice("INV-1005", "V100", "PO-5005", "20260912", [("P-500", 50, "30.00")]),
    invoice(
        "INV-1006", "V100", "PO-5006", "20260914", [("C-600", 20, "15.00"), ("FRT", 1, "85.00")]
    ),
    invoice(
        "INV-1007", "V200", "PO-5004", "20260929", [("B-400", 100, "4.50"), ("H-410", 50, "10.00")]
    ),
    invoice("INV-1008", "V200", "PO-5008", "20260915", [("W-200B", 30, "18.00")]),
    invoice("INV-1009", "V200", "PO-5009", "20260918", [("T-900", 120, "10.00")]),
    invoice("INV-1010", "V100", "PO-9999", "20260920", [("W-100", 10, "12.00")]),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "01_purchase_orders.850.edi").write_text(
        build("850", PURCHASE_ORDERS, sender="BUYER", receiver="VENDORS", control=101)
    )
    (OUT / "02_ship_notices.856.edi").write_text(build("856", SHIP_NOTICES, control=201))
    (OUT / "03_invoices.810.edi").write_text(build("810", INVOICES, control=301))
    print(f"wrote 3 interchanges to {OUT}")


if __name__ == "__main__":
    main()
