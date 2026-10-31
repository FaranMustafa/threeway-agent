"""SQLite persistence. EDI documents are stored as validated JSON next to indexed keys;
master data (vendors, contract prices, receiving log) is normalized."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from threeway.models import CaseStatus, Invoice, PurchaseOrder, ShipNotice

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    terms TEXT NOT NULL DEFAULT '',
    price_tolerance_bps INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contract_prices (
    vendor_id TEXT NOT NULL REFERENCES vendors(vendor_id),
    sku TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT
);
CREATE TABLE IF NOT EXISTS receipts (
    po_number TEXT NOT NULL,
    sku TEXT NOT NULL,
    qty_received INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS vendor_notes (
    vendor_id TEXT NOT NULL REFERENCES vendors(vendor_id),
    noted_at TEXT NOT NULL,
    author TEXT NOT NULL,
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    doc_type TEXT NOT NULL CHECK (doc_type IN ('850', '856', '810')),
    doc_key TEXT NOT NULL,
    po_number TEXT NOT NULL,
    vendor_id TEXT,
    body TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (doc_type, doc_key)
);
CREATE INDEX IF NOT EXISTS documents_po ON documents(po_number);
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    invoice_number TEXT NOT NULL,
    planner TEXT NOT NULL,
    status TEXT NOT NULL,
    exceptions TEXT NOT NULL,
    resolution TEXT,
    trace TEXT NOT NULL,
    usage TEXT NOT NULL DEFAULT '{}',
    reviewer TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cases_invoice ON cases(invoice_number);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- master data -------------------------------------------------------------------

    def upsert_vendor(self, vendor_id: str, name: str, terms: str, price_tolerance_bps: int):
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO vendors VALUES (?, ?, ?, ?)",
                (vendor_id, name, terms, price_tolerance_bps),
            )

    def add_contract_price(self, vendor_id, sku, unit_price_cents, effective_from, effective_to):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO contract_prices VALUES (?, ?, ?, ?, ?)",
                (vendor_id, sku, unit_price_cents, effective_from, effective_to),
            )

    def add_receipt(self, po_number, sku, qty_received, received_at, note=""):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                (po_number, sku, qty_received, received_at, note),
            )

    def add_vendor_note(self, vendor_id, noted_at, author, note):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO vendor_notes VALUES (?, ?, ?, ?)", (vendor_id, noted_at, author, note)
            )

    def vendor(self, vendor_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM vendors WHERE vendor_id = ?", (vendor_id,)).fetchone()
        return dict(row) if row else None

    def contract_price(self, vendor_id: str, sku: str, as_of: date) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM contract_prices
                   WHERE vendor_id = ? AND sku = ? AND effective_from <= ?
                     AND (effective_to IS NULL OR effective_to >= ?)
                   ORDER BY effective_from DESC LIMIT 1""",
                (vendor_id, sku, as_of.isoformat(), as_of.isoformat()),
            ).fetchone()
        return dict(row) if row else None

    def receipts(self, po_number: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM receipts WHERE po_number = ? ORDER BY received_at", (po_number,)
            ).fetchall()
        return [dict(r) for r in rows]

    def vendor_notes(self, vendor_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM vendor_notes WHERE vendor_id = ? ORDER BY noted_at DESC",
                (vendor_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- EDI documents -----------------------------------------------------------------

    def save_document(self, doc: PurchaseOrder | ShipNotice | Invoice) -> str:
        match doc:
            case PurchaseOrder():
                doc_type, key, vendor = "850", doc.po_number, doc.vendor_id
            case ShipNotice():
                doc_type, key, vendor = "856", doc.asn_id, None
            case Invoice():
                doc_type, key, vendor = "810", doc.invoice_number, doc.vendor_id
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?, ?)",
                (doc_type, key, doc.po_number, vendor, doc.model_dump_json(), _now()),
            )
        return doc_type

    def _docs(self, where: str, args: tuple) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                f"SELECT doc_type, body FROM documents WHERE {where} ORDER BY doc_key", args
            ).fetchall()

    def purchase_order(self, po_number: str) -> PurchaseOrder | None:
        rows = self._docs("doc_type = '850' AND doc_key = ?", (po_number,))
        return PurchaseOrder.model_validate_json(rows[0]["body"]) if rows else None

    def ship_notices(self, po_number: str) -> list[ShipNotice]:
        rows = self._docs("doc_type = '856' AND po_number = ?", (po_number,))
        return [ShipNotice.model_validate_json(r["body"]) for r in rows]

    def invoice(self, invoice_number: str) -> Invoice | None:
        rows = self._docs("doc_type = '810' AND doc_key = ?", (invoice_number,))
        return Invoice.model_validate_json(rows[0]["body"]) if rows else None

    def invoices(self, vendor_id: str | None = None) -> list[Invoice]:
        if vendor_id:
            rows = self._docs("doc_type = '810' AND vendor_id = ?", (vendor_id,))
        else:
            rows = self._docs("doc_type = '810'", ())
        return [Invoice.model_validate_json(r["body"]) for r in rows]

    # ---- cases -------------------------------------------------------------------------

    def create_case(
        self,
        invoice_number: str,
        planner: str,
        status: CaseStatus,
        exceptions: list[dict],
        resolution: dict | None,
        trace: list[dict],
        usage: dict,
    ) -> str:
        case_id = f"case_{uuid.uuid4().hex[:12]}"
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO cases (case_id, invoice_number, planner, status, exceptions,
                       resolution, trace, usage, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    case_id,
                    invoice_number,
                    planner,
                    status.value,
                    json.dumps(exceptions),
                    json.dumps(resolution) if resolution else None,
                    json.dumps(trace, default=str),
                    json.dumps(usage),
                    now,
                    now,
                ),
            )
        return case_id

    def case(self, case_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        return self._case_row(row) if row else None

    def cases(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM cases WHERE status = ? ORDER BY created_at DESC", (status,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM cases ORDER BY created_at DESC").fetchall()
        return [self._case_row(r, with_trace=False) for r in rows]

    def review_case(self, case_id: str, status: CaseStatus, reviewer: str, note: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """UPDATE cases SET status = ?, reviewer = ?, review_note = ?, updated_at = ?
                   WHERE case_id = ? AND status = ?""",
                (status.value, reviewer, note, _now(), case_id, CaseStatus.AWAITING_HUMAN.value),
            )
        return cur.rowcount == 1

    @staticmethod
    def _case_row(row: sqlite3.Row, with_trace: bool = True) -> dict[str, Any]:
        out = dict(row)
        for key in ("exceptions", "resolution", "trace", "usage"):
            if out.get(key) is not None:
                out[key] = json.loads(out[key])
        if not with_trace:
            out.pop("trace", None)
        return out
