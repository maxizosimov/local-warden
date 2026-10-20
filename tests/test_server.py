"""API auth: the agent token can propose purchases, never approve or change policy."""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("WARDEN_DATA", str(tmp_path))
    import warden.server as server
    server = importlib.reload(server)
    client = TestClient(server.app, base_url="http://127.0.0.1:8787")
    return server, client


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_writes_need_owner(srv):
    server, c = srv
    assert c.put("/api/policy", json={"risk_level": 4}).status_code == 401
    assert c.put("/api/policy", json={"risk_level": 4}, headers=H(server.AGENT_TOKEN)).status_code == 401
    assert c.post("/api/approvals/1/approve").status_code == 401
    assert c.get("/api/state").status_code == 401
    r = c.put("/api/policy", json={"risk_level": 2}, headers={"X-Warden-Owner": server.OWNER_TOKEN})
    assert r.status_code == 200 and r.json()["risk_level"] == 2


def test_agent_cannot_approve_its_own_purchase(srv):
    server, c = srv
    r = c.post("/api/intents", json={"merchant": "demo-store", "sku": "CHR-200"}, headers=H(server.AGENT_TOKEN))
    assert r.json()["status"] == "awaiting_approval"
    aid = r.json()["approval_id"]
    assert c.post(f"/api/approvals/{aid}/approve", headers=H(server.AGENT_TOKEN)).status_code == 401
    assert c.post(f"/api/approvals/{aid}/approve", headers=H(server.OWNER_TOKEN)).json()["status"] == "handed_off"


def test_agent_cannot_claim_source(srv):
    server, c = srv
    c.post("/api/intents", json={"merchant": "demo-store", "sku": "PEN-510", "source": "user"},
           headers=H(server.AGENT_TOKEN))
    ev = server.hub.ledger.list(kind="policy_decision")[0]
    assert ev["data"]["intent"]["source"] == "agent"


def test_foreign_host_blocked(srv):
    _, c = srv
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_handoff_paid_via_api(srv):
    server, c = srv
    o = {"X-Warden-Owner": server.OWNER_TOKEN}
    c.put("/api/policy", json={"risk_level": 2}, headers=o)
    r = c.post("/api/intents", json={"merchant": "demo-store", "sku": "PEN-510"}, headers=H(server.AGENT_TOKEN)).json()
    assert r["status"] == "handed_off"
    assert c.get(r["cart_url"]).status_code == 200
    assert c.post(f"/api/orders/{r['order_id']}/paid", json={"amount": 14.49}, headers=H(server.AGENT_TOKEN)).status_code == 401
    p = c.post(f"/api/orders/{r['order_id']}/paid", json={"amount": 14.49}, headers=o).json()
    assert p["outcome"] == "as_proposed"
    assert c.get("/api/track-record", headers=o).json()["overall"]["as_proposed"] == 1
