"""HTTP API + dashboard. Binds to localhost by default - your data stays on your machine."""
from __future__ import annotations

import csv
import io
import json
import os
import secrets
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from .hub import Hub
from .merchants import REGISTRY

DATA = Path(os.getenv("WARDEN_DATA", "data"))
hub = Hub(DATA)

tok_file = DATA / "agent_token"
if not tok_file.exists():
    tok_file.write_text(secrets.token_urlsafe(24))
AGENT_TOKEN = os.getenv("WARDEN_AGENT_TOKEN") or tok_file.read_text().strip()

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    hub.start_background(float(os.getenv("WARDEN_TICK_SECONDS", "15")),
                         jitter_demo=os.getenv("WARDEN_DEMO_JITTER", "1") == "1")
    hub.log("hub_started", "hub", risk_level=hub.policy.risk_level)
    yield
    hub.stop()


app = FastAPI(title="Warden", version="0.1.0", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"


def _agent(auth: str | None) -> str:
    if not auth or auth.removeprefix("Bearer ").strip() != AGENT_TOKEN:
        raise HTTPException(401, "agent token required (see data/agent_token)")
    return "external-agent"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state():
    return hub.snapshot()


# ---- requests ------------------------------------------------------------
@app.post("/api/tasks")
def create_task(body: dict = Body(...)):
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    return hub.submit_text(text)


@app.post("/api/buy")
def buy_sku(body: dict = Body(...)):
    """Buy a specific catalog item (e.g. after research). Still goes through policy."""
    return hub.submit_intent({**body, "source": "user"}, "user")


@app.post("/api/intents")
def external_intent(body: dict = Body(...), authorization: str | None = Header(None)):
    """Any external agent can ask the hub to buy something. The hub decides."""
    return hub.submit_intent(body, _agent(authorization))


# ---- approvals -------------------------------------------------------------
@app.post("/api/approvals/{aid}/{action}")
def decide(aid: int, action: str):
    if action not in ("approve", "reject"):
        raise HTTPException(400)
    return hub.decide(aid, action == "approve")


# ---- policy ------------------------------------------------------------
@app.get("/api/policy")
def get_policy():
    return hub.policy.to_dict()


@app.put("/api/policy")
def put_policy(body: dict = Body(...)):
    return hub.set_policy(body).to_dict()


# ---- monitors ------------------------------------------------------------
@app.post("/api/monitors")
def add_monitor(body: dict = Body(...)):
    merchant = body.get("merchant", "demo-store")
    sku = body.get("sku") or body.get("url")
    a = REGISTRY.get(merchant)
    p = a.get_product(sku) if a and sku else None
    if not p:
        raise HTTPException(404, "product not found / price unreadable")
    mid = hub.add_monitor(merchant, sku, p.name, body.get("max_price"), body.get("min_discount_pct"),
                          int(body.get("quantity", 1)), body.get("action", "notify" if not a.can_purchase else "buy"))
    return {"monitor_id": mid, "product": p.to_dict()}


@app.delete("/api/monitors/{mid}")
def del_monitor(mid: int):
    hub.cancel_monitor(mid)
    return {"ok": True}


@app.post("/api/monitors/tick")
def tick():
    return {"fired": hub.tick_monitors()}


# ---- ledger / data feed -------------------------------------------------
@app.get("/api/ledger")
def ledger(limit: int = 200, kind: str | None = None):
    return hub.ledger.list(limit, kind)


@app.get("/api/ledger/verify")
def verify():
    return hub.ledger.verify()


@app.get("/api/feed")
def feed(since_id: int = 0):
    """Pull-based data feed: poll with the last id you saw."""
    rows = hub.ledger.db.execute("SELECT * FROM events WHERE id>? ORDER BY id ASC LIMIT 500", (since_id,))
    return [{**dict(r), "data": json.loads(r["data"])} for r in rows]


@app.get("/api/export/orders.csv", response_class=PlainTextResponse)
def export_orders():
    rows = hub.snapshot()["orders"]
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    return buf.getvalue()


# ---- catalog + demo controls ---------------------------------------------
@app.get("/api/catalog")
def catalog(q: str = ""):
    store = REGISTRY["demo-store"]
    items = store.search(q) if q else list(store.products.values())
    return [p.to_dict() for p in items]


@app.post("/api/demo/sale")
def demo_sale(body: dict = Body(...)):
    p = REGISTRY["demo-store"].set_sale(body["sku"], float(body.get("pct_off", 25)))
    hub.log("demo_price_change", "demo", sku=p.sku, price=p.price, pct_off=body.get("pct_off", 25))
    fired = hub.tick_monitors()
    return {"product": p.to_dict(), "fired": fired}


@app.post("/api/demo/reset")
def demo_reset():
    for p in REGISTRY["demo-store"].products.values():
        p.price = p.list_price
    return {"ok": True}


def main():
    import uvicorn
    host = os.getenv("WARDEN_HOST", "127.0.0.1")
    port = int(os.getenv("WARDEN_PORT", "8787"))
    print(f"\n  Warden hub -> http://{host}:{port}\n  Agent token: {AGENT_TOKEN}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
