import pytest
from fastapi.testclient import TestClient

from threeway import api
from threeway.agent.planners import BaselinePlanner
from threeway.edi.builder import build


@pytest.fixture
def client(store):
    api.app.dependency_overrides[api.get_store] = lambda: store
    api.app.dependency_overrides[api.get_planner] = BaselinePlanner
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def test_upload_invoice_matches_on_arrival(client):
    raw = build(
        "810",
        [
            [
                "BIG*20260930*INV-2000**PO-5003",
                "N1*VN*N*92*V200",
                "IT1*1*40*EA*25.00**VP*G-300",
                "TDS*100000",
            ]
        ],
    )
    r = client.post("/edi", content=raw, headers={"content-type": "text/plain"})
    assert r.status_code == 201
    doc = r.json()["documents"][0]
    assert doc["invoice_number"] == "INV-2000" and doc["exceptions"] == []


def test_bad_edi_is_422(client):
    r = client.post("/edi", content="nope", headers={"content-type": "text/plain"})
    assert r.status_code == 422


def test_match_view(client):
    r = client.get("/invoices/INV-1009/match").json()
    assert r["rule_verified_amount"] == "$0.00"
    assert r["exceptions"][0]["kind"] == "uom_mismatch"


def test_investigate_and_human_review(client):
    case = client.post("/invoices/INV-1009/investigate?planner=baseline").json()
    assert case["status"] == "awaiting_human"
    assert case["trace"][-1]["event"] == "route"

    queue = client.get("/cases", params={"status": "awaiting_human"}).json()
    assert [c["case_id"] for c in queue] == [case["case_id"]]

    body = {"approve": False, "reviewer": "ap.lead", "note": "billed in EA, ordered in CS"}
    reviewed = client.post(f"/cases/{case['case_id']}/review", json=body).json()
    assert reviewed["status"] == "rejected" and reviewed["reviewer"] == "ap.lead"
    again = client.post(f"/cases/{case['case_id']}/review", json=body)
    assert again.status_code == 409


def test_unknown_invoice_404(client):
    assert client.post("/invoices/NOPE/investigate").status_code == 404
