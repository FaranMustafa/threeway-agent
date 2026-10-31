"""Load master data and EDI files into a store."""

from __future__ import annotations

import json
from pathlib import Path

from threeway.db import Store
from threeway.edi.x12 import Document, parse

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def ingest(store: Store, raw: str) -> list[Document]:
    docs = parse(raw)
    for doc in docs:
        store.save_document(doc)
    return docs


def load_master(store: Store, path: Path = DATA_DIR / "master.json") -> None:
    data = json.loads(path.read_text())
    for v in data["vendors"]:
        store.upsert_vendor(v["vendor_id"], v["name"], v["terms"], v["price_tolerance_bps"])
    for c in data["contract_prices"]:
        store.add_contract_price(
            c["vendor_id"], c["sku"], c["unit_price_cents"], c["effective_from"], c["effective_to"]
        )
    for r in data["receipts"]:
        store.add_receipt(r["po_number"], r["sku"], r["qty_received"], r["received_at"], r["note"])
    for n in data["vendor_notes"]:
        store.add_vendor_note(n["vendor_id"], n["noted_at"], n["author"], n["note"])


def seed(store: Store, data_dir: Path = DATA_DIR) -> int:
    load_master(store, data_dir / "master.json")
    count = 0
    for path in sorted((data_dir / "edi").glob("*.edi")):
        count += len(ingest(store, path.read_text()))
    return count
