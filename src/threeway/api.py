"""HTTP service: ingest EDI, inspect exceptions, run the agent, and let a person sign off."""

from __future__ import annotations

import os
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

from threeway.agent.planners import BaselinePlanner, ClaudePlanner, Planner
from threeway.agent.runner import investigate
from threeway.db import Store
from threeway.edi.x12 import X12Error
from threeway.matching import match_invoice
from threeway.models import CaseStatus, fmt
from threeway.seed import ingest

app = FastAPI(
    title="ThreeWay",
    description="Agentic resolution of EDI three-way-match exceptions in accounts payable.",
    version="0.1.0",
)

_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(os.environ.get("THREEWAY_DB", "threeway.db"))
    return _store


def get_planner(planner: Literal["claude", "baseline"] = "claude") -> Planner:
    return ClaudePlanner() if planner == "claude" else BaselinePlanner()


StoreDep = Annotated[Store, Depends(get_store)]


class ReviewIn(BaseModel):
    approve: bool
    reviewer: str
    note: str = ""


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/edi", status_code=201)
def upload_edi(store: StoreDep, raw: Annotated[str, Body(media_type="text/plain")]):
    """Ingest one X12 interchange (850, 856 or 810). Invoices are matched on arrival."""
    try:
        docs = ingest(store, raw)
    except X12Error as exc:
        raise HTTPException(422, f"invalid X12: {exc}") from exc
    out = []
    for doc in docs:
        item = {"type": type(doc).__name__, "po_number": doc.po_number}
        if hasattr(doc, "invoice_number"):
            match = match_invoice(store, doc)
            item |= {
                "invoice_number": doc.invoice_number,
                "exceptions": [e.model_dump() for e in match.exceptions],
            }
        out.append(item)
    return {"documents": out}


@app.get("/invoices/{invoice_number}/match")
def invoice_match(invoice_number: str, store: StoreDep):
    invoice = store.invoice(invoice_number)
    if invoice is None:
        raise HTTPException(404, "invoice not found")
    match = match_invoice(store, invoice)
    return {
        "invoice_number": invoice_number,
        "line_total": fmt(invoice.line_total_cents),
        "rule_verified_amount": fmt(match.supported_amount_cents),
        "exceptions": [e.model_dump() for e in match.exceptions],
    }


@app.post("/invoices/{invoice_number}/investigate", status_code=201)
def run_agent(
    invoice_number: str, store: StoreDep, planner: Annotated[Planner, Depends(get_planner)]
):
    try:
        outcome = investigate(store, invoice_number, planner)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return store.case(outcome.case_id)


@app.get("/cases")
def list_cases(store: StoreDep, status: Annotated[CaseStatus | None, Query()] = None):
    return store.cases(status.value if status else None)


@app.get("/cases/{case_id}")
def get_case(case_id: str, store: StoreDep):
    case = store.case(case_id)
    if case is None:
        raise HTTPException(404, "case not found")
    return case


@app.post("/cases/{case_id}/review")
def review(case_id: str, body: ReviewIn, store: StoreDep):
    """A person approves or rejects a case the agent could not post on its own."""
    status = CaseStatus.APPROVED if body.approve else CaseStatus.REJECTED
    if not store.review_case(case_id, status, body.reviewer, body.note):
        raise HTTPException(409, "case is not awaiting human review")
    return store.case(case_id)
