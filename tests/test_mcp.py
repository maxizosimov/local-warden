"""MCP server against a real hub over HTTP, plus a raw stdio protocol check."""
import importlib
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest
import uvicorn


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("WARDEN_DATA", str(tmp_path))
    import warden.server as server
    server = importlib.reload(server)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.02)
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("WARDEN_URL", url)
    import warden.mcp_server as m
    m = importlib.reload(m)

    def owner(method, path, body=None):
        req = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"X-Warden-Owner": server.OWNER_TOKEN, "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read())

    yield m, server, owner, url, tmp_path
    srv.should_exit = True
    t.join(5)


def test_purchase_handoff_flow(live):
    m, server, owner, *_ = live
    owner("PUT", "/api/policy", {"risk_level": 2})
    assert m.get_policy()["remaining"]["today"] == 200
    sku = m.search_products("gel pens")[0]["sku"]
    r = m.request_purchase(sku, "User asked for pens for the office.")
    assert r["status"] == "handed_off" and "user pays" in r["next_step"].lower()
    row = server.hub.snapshot()["orders"][0]
    assert json.loads(row["rationale"])[0] == "Agent: User asked for pens for the office."
    assert server.hub.ledger.list(kind="policy_decision")[0]["data"]["intent"]["source"] == "agent"
    owner("POST", f"/api/orders/{r['order_id']}/paid", {"amount": 14.49})
    s = m.check_request(order_id=r["order_id"])
    assert s["outcome"] == "as_proposed" and s["paid_by"] == "you"
    assert m.track_record()["overall"]["as_proposed"] == 1


def test_approval_followup(live):
    m, server, owner, *_ = live
    r = m.request_purchase("CHR-200", "Cheapest task chair in stock.")
    assert r["status"] == "awaiting_approval"
    assert m.check_request(approval_id=r["approval_id"])["status"] == "awaiting_approval"
    owner("POST", f"/api/approvals/{r['approval_id']}/approve")
    s = m.check_request(approval_id=r["approval_id"])
    assert s["status"] == "handed_off" and s["approval_id"] == r["approval_id"]


def test_agent_cannot_escalate(live):
    m, *_ = live
    with pytest.raises(m.HubError, match="401"):
        m._call("PUT", "/api/policy", {"risk_level": 4})
    with pytest.raises(m.HubError, match="401"):
        m._call("POST", "/api/approvals/1/approve")


def test_watch_price_and_denial(live):
    m, *_ = live
    w = m.watch_price("GRL-400", min_discount_pct=25, action="buy")
    assert w["monitor_id"] and w["action"] == "buy"
    with pytest.raises(m.HubError, match="400"):
        m.watch_price("GRL-400")  # no condition
    with pytest.raises(m.HubError):
        m.watch_price("file:///etc/passwd", max_price=1, merchant="web")
    assert m.request_purchase("GFT-700", "gift")["status"] == "denied"


def test_refuses_owner_token(live, monkeypatch):
    m, server, *_ = live
    monkeypatch.setenv("WARDEN_AGENT_TOKEN", server.OWNER_TOKEN)
    with pytest.raises(m.HubError, match="owner token"):
        m.get_policy()


def test_stdio_protocol(live):
    _, _, _, url, data = live
    p = subprocess.Popen([sys.executable, "-m", "warden.mcp_server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True,
                         env={**__import__("os").environ, "WARDEN_URL": url, "WARDEN_DATA": str(data)})

    def rpc(i, method, params):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())

    try:
        init = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}})
        assert init["result"]["serverInfo"]["name"] == "warden"
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        p.stdin.flush()
        tools = {t["name"] for t in rpc(2, "tools/list", {})["result"]["tools"]}
        assert tools == {"get_policy", "search_products", "request_purchase", "check_request",
                         "watch_price", "track_record"}
        res = rpc(3, "tools/call", {"name": "request_purchase",
                                    "arguments": {"sku": "PEN-510", "reason": "pens"}})["result"]
        assert not res.get("isError") and "awaiting_approval" in json.dumps(res)
    finally:
        p.kill()
