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


def test_hosted_hostname_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("WARDEN_DATA", str(tmp_path))
    monkeypatch.setenv("WARDEN_ALLOWED_HOSTS", "warden-abc.onrender.com")
    import warden.server as server
    server = importlib.reload(server)
    c = TestClient(server.app, base_url="https://warden-abc.onrender.com")
    assert c.get("/").status_code == 200
    assert c.get("/api/state").status_code == 401  # still needs the owner token
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_demo_checkout_is_simulated_and_records_payment(srv):
    server, c = srv
    r = c.post("/api/intents", json={"merchant": "demo-store", "sku": "CHR-200"}, headers=H(server.AGENT_TOKEN))
    approved = c.post(f"/api/approvals/{r.json()['approval_id']}/approve", headers=H(server.OWNER_TOKEN)).json()
    page = c.get(approved["cart_url"])
    assert page.status_code == 200 and "Simulated store" in page.text and "4242 4242 4242 4242" in page.text
    assert "nothing is charged" in page.text and "<script>" in page.text
    # What the page's Pay button sends: the amount only, never the card.
    paid = c.post(f"/api/orders/{approved['order_id']}/paid", json={"amount": approved["expected_total"]},
                  headers=H(server.OWNER_TOKEN)).json()
    assert paid["outcome"] == "as_proposed" and paid["merchant_order_id"].startswith("DEMO-")


def test_demo_checkout_escapes_and_bounds(srv):
    _, c = srv
    assert c.get("/demo/cart/NOPE").status_code == 404
    assert "×99" in c.get("/demo/cart/PEN-510?qty=5000").text


def test_wallet_keeps_masked_details_only(srv):
    server, c = srv
    O = {"X-Warden-Owner": server.OWNER_TOKEN}
    assert c.get("/api/wallet").status_code == 401
    assert c.get("/api/wallet", headers=H(server.AGENT_TOKEN)).status_code == 401  # agents never see it
    assert {w["kind"] for w in c.get("/api/wallet", headers=O).json()} == {"login", "card", "bank"}
    r = c.post("/api/wallet", json={"kind": "login", "store": "staples", "login": "me@example.com",
                                    "password": "hunter2"}, headers=O).json()
    assert "password" not in r and "hunter2" not in (server.DATA / "wallet.json").read_text()
    assert c.post("/api/wallet", json={"kind": "card", "label": "Visa", "last4": "4242424242424242"},
                  headers=O).status_code == 400
    c.delete(f"/api/wallet/{r['id']}", headers=O)
    assert all(w["id"] != r["id"] for w in c.get("/api/wallet", headers=O).json())


def test_guided_demo_script(srv):
    """The steps the dashboard's Demo tour runs, in order."""
    server, c = srv
    O = {"X-Warden-Owner": server.OWNER_TOKEN}
    post = lambda path, body=None: c.post(path, json=body or {}, headers=O).json()
    c.put("/api/policy", json={"risk_level": 3, "pay_merchants": ["demo-store"]}, headers=O)
    post("/api/tasks", {"text": "buy a chair"})
    assert post("/api/demo/clear")["ok"]
    s = c.get("/api/state", headers=O).json()
    assert s["policy"]["risk_level"] == 1 and s["policy"]["pay_merchants"] == []
    assert not [a for a in s["approvals"] if a["status"] == "pending"]

    r = post("/api/tasks", {"text": "buy gel pens"})
    assert r["status"] == "awaiting_approval"
    r = post(f"/api/approvals/{r['approval_id']}/approve")
    assert r["status"] == "handed_off"
    assert post(f"/api/orders/{r['order_id']}/paid", {"amount": r["expected_total"]})["outcome"] == "as_proposed"

    assert post("/api/tasks", {"text": "buy the propane grill when it's 25% off"})["status"] == "monitoring"
    fired = post("/api/demo/sale", {"sku": "GRL-400", "pct_off": 25})["fired"]
    assert fired and fired[0]["status"] == "awaiting_approval"

    c.put("/api/policy", json={"risk_level": 2}, headers=O)
    assert post("/api/tasks", {"text": "buy gel pens"})["status"] == "handed_off"
    assert post("/api/tasks", {"text": "buy 50 gel pens"})["status"] == "denied"
    c.put("/api/policy", json={"pay_merchants": ["demo-store"]}, headers=O)
    assert post("/api/tasks", {"text": "buy gel pens"})["status"] == "ordered"


def test_order_confirmations_checked_against_approvals(srv):
    server, c = srv
    O = {"X-Warden-Owner": server.OWNER_TOKEN}
    post = lambda path, body=None: c.post(path, json=body or {}, headers=O).json()
    r = post("/api/tasks", {"text": "buy gel pens"})
    r = post(f"/api/approvals/{r['approval_id']}/approve")
    post(f"/api/orders/{r['order_id']}/paid", {"amount": r["expected_total"]})
    r = post("/api/tasks", {"text": "buy coffee"})
    r = post(f"/api/approvals/{r['approval_id']}/approve")
    post(f"/api/orders/{r['order_id']}/paid", {"amount": 30})
    assert post("/api/demo/unapproved-order")["result"] == "unapproved"
    got = {x["item"]: x for x in c.get("/api/state", headers=O).json()["confirmations"]}
    assert got["Gel Pens, 24 Pack"]["result"] == "matched"
    assert got["Coffee Beans 2lb"]["result"] == "different" and "$30.00" in got["Coffee Beans 2lb"]["note"]
    assert got["Printer Ink Combo Pack"]["result"] == "unapproved" and got["Printer Ink Combo Pack"]["order_id"] is None
