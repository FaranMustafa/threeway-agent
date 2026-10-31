"""A small, strict ANSI X12 reader for the three documents AP cares about.

Supports 850 (purchase order), 856 (advance ship notice) and 810 (invoice).
Delimiters are read from the ISA header, as the standard requires, instead of
being assumed. Several ST/SE transaction sets per interchange are allowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from threeway.models import Invoice, Line, PurchaseOrder, ShipmentLine, ShipNotice, to_cents


class X12Error(ValueError):
    pass


@dataclass(frozen=True)
class Segment:
    tag: str
    elements: list[str]

    def get(self, index: int, default: str = "") -> str:
        """1-based element access, matching how X12 specs number elements (BEG03 -> get(3))."""
        return self.elements[index - 1] if 0 < index <= len(self.elements) else default


@dataclass(frozen=True)
class TransactionSet:
    code: str
    control_number: str
    segments: list[Segment]


Document = PurchaseOrder | ShipNotice | Invoice


def tokenize(raw: str) -> list[Segment]:
    raw = raw.strip().lstrip("﻿")
    if not raw.startswith("ISA") or len(raw) < 106:
        raise X12Error("interchange must start with a 106-character ISA segment")
    element_sep = raw[3]
    segment_term = raw[105]
    segments = []
    for chunk in raw.split(segment_term):
        chunk = chunk.strip()
        if not chunk:
            continue
        tag, *elements = chunk.split(element_sep)
        segments.append(Segment(tag, elements))
    return segments


def transaction_sets(segments: list[Segment]) -> list[TransactionSet]:
    sets: list[TransactionSet] = []
    current: list[Segment] | None = None
    header: Segment | None = None
    for seg in segments:
        if seg.tag == "ST":
            if current is not None:
                raise X12Error(f"ST {seg.get(2)} opened before SE closed {header.get(2)}")
            current, header = [], seg
        elif seg.tag == "SE":
            if current is None or header is None:
                raise X12Error("SE without matching ST")
            # SE01 counts every segment in the set including ST and SE.
            if int(seg.get(1)) != len(current) + 2:
                raise X12Error(
                    f"SE01 says {seg.get(1)} segments, found {len(current) + 2} "
                    f"in set {header.get(2)}"
                )
            if seg.get(2) != header.get(2):
                raise X12Error(f"SE02 {seg.get(2)} does not match ST02 {header.get(2)}")
            sets.append(TransactionSet(header.get(1), header.get(2), current))
            current, header = None, None
        elif current is not None:
            current.append(seg)
    if current is not None:
        raise X12Error("transaction set not closed with SE")
    return sets


def _date(ccyymmdd: str) -> date:
    try:
        return datetime.strptime(ccyymmdd, "%Y%m%d").date()
    except ValueError as exc:
        raise X12Error(f"bad date {ccyymmdd!r}") from exc


def _product_id(seg: Segment, start: int) -> str:
    """Find the VP (vendor part) or BP (buyer part) id in qualifier/value pairs."""
    for i in range(start, len(seg.elements) + 1, 2):
        if seg.get(i) in ("VP", "BP"):
            return seg.get(i + 1)
    raise X12Error(f"{seg.tag} segment has no VP/BP product id")


def _vendor_id(segs: list[Segment]) -> str:
    for seg in segs:
        if seg.tag == "N1" and seg.get(1) == "VN":
            return seg.get(4)
    raise X12Error("missing N1*VN vendor loop")


def _parse_850(ts: TransactionSet) -> PurchaseOrder:
    beg = next(s for s in ts.segments if s.tag == "BEG")
    lines = [
        Line(
            line_no=int(s.get(1)),
            qty=int(s.get(2)),
            uom=s.get(3) or "EA",
            unit_price_cents=to_cents(s.get(4)),
            sku=_product_id(s, 6),
        )
        for s in ts.segments
        if s.tag == "PO1"
    ]
    return PurchaseOrder(
        po_number=beg.get(3),
        vendor_id=_vendor_id(ts.segments),
        order_date=_date(beg.get(5)),
        lines=lines,
    )


def _parse_856(ts: TransactionSet) -> ShipNotice:
    bsn = next(s for s in ts.segments if s.tag == "BSN")
    prf = next((s for s in ts.segments if s.tag == "PRF"), None)
    if prf is None:
        raise X12Error("856 missing PRF purchase order reference")
    lines: list[ShipmentLine] = []
    pending: dict | None = None
    for seg in ts.segments:
        if seg.tag == "LIN":
            sku = _product_id(seg, 2)
            # LIN may carry a second pair 'SU' = substitute-for, used when the vendor ships an
            # equivalent part.
            substitute_for = None
            for i in range(2, len(seg.elements) + 1, 2):
                if seg.get(i) == "SU":
                    substitute_for = seg.get(i + 1)
            pending = {"sku": sku, "substitute_for": substitute_for}
        elif seg.tag == "SN1" and pending is not None:
            lines.append(ShipmentLine(qty_shipped=int(seg.get(2)), **pending))
            pending = None
    return ShipNotice(
        asn_id=bsn.get(2), po_number=prf.get(1), ship_date=_date(bsn.get(3)), lines=lines
    )


def _parse_810(ts: TransactionSet) -> Invoice:
    big = next(s for s in ts.segments if s.tag == "BIG")
    tds = next((s for s in ts.segments if s.tag == "TDS"), None)
    if tds is None:
        raise X12Error("810 missing TDS total")
    lines = [
        Line(
            line_no=int(s.get(1)),
            qty=int(s.get(2)),
            uom=s.get(3) or "EA",
            unit_price_cents=to_cents(s.get(4)),
            sku=_product_id(s, 6),
        )
        for s in ts.segments
        if s.tag == "IT1"
    ]
    return Invoice(
        invoice_number=big.get(2),
        invoice_date=_date(big.get(1)),
        po_number=big.get(4),
        vendor_id=_vendor_id(ts.segments),
        lines=lines,
        # TDS01 is an implied-decimal amount: 123456 means $1,234.56.
        stated_total_cents=int(tds.get(1)),
    )


PARSERS = {"850": _parse_850, "856": _parse_856, "810": _parse_810}


def parse(raw: str) -> list[Document]:
    docs: list[Document] = []
    for ts in transaction_sets(tokenize(raw)):
        parser = PARSERS.get(ts.code)
        if parser is None:
            raise X12Error(f"unsupported transaction set {ts.code}")
        try:
            docs.append(parser(ts))
        except StopIteration as exc:
            raise X12Error(f"{ts.code} {ts.control_number} is missing a header segment") from exc
    return docs
