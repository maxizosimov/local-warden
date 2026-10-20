"""HTTP API + dashboard. Binds to localhost by default - your data stays on your machine."""
from __future__ import annotations

import csv
import io
import html
import json
import os
import secrets
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from .hub import Hub
from .merchants import REGISTRY

DATA = Path(os.getenv("WARDEN_DATA", "data"))
hub = Hub(DATA)


def _token(name: str, env: str) -> str:
    f = DATA / name
    if not f.exists():
        f.write_text(secrets.token_urlsafe(24))
        f.chmod(0o600)
    return os.getenv(env) or f.read_text().strip()


# Two separate credentials: the agent token can only submit purchase intents and
# read the feed. The owner token (you, via the dashboard) approves, pays and
# changes policy. Give your agent the first, never the second.
AGENT_TOKEN = _token("agent_token", "WARDEN_AGENT_TOKEN")
OWNER_TOKEN = _token("owner_token", "WARDEN_OWNER_TOKEN")

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    hub.start_background(float(os.getenv("WARDEN_TICK_SECONDS", "15")),
                         jitter_demo=os.getenv("WARDEN_DEMO_JITTER", "1") == "1")
    hub.log("hub_started", "hub", risk_level=hub.policy.risk_level)
    yield
    hub.stop()


app = FastAPI(title="Warden", version="0.2.0", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", os.getenv("WARDEN_HOST", "127.0.0.1")}


@app.middleware("http")
async def local_host_only(request: Request, call_next):
    # Blocks DNS-rebinding: a web page can't reach the hub through a hostname it controls.
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in LOCAL_HOSTS:
        return PlainTextResponse("forbidden host", status_code=403)
    return await call_next(request)


def _bearer(auth: str | None, owner: str | None) -> bytes:
    return (owner or (auth or "").removeprefix("Bearer ")).strip().encode()


def owner(authorization: str | None = Header(None), x_warden_owner: str | None = Header(None)) -> str:
    # The dashboard sends X-Warden-Owner. A custom header can't be sent cross-site
    # without a CORS preflight (which we never allow), so this also stops CSRF.
    if not secrets.compare_digest(_bearer(authorization, x_warden_owner), OWNER_TOKEN.encode()):
        raise HTTPException(401, "owner token required (see data/owner_token)")
    return "user"


def agent(authorization: str | None = Header(None), x_warden_owner: str | None = Header(None)) -> str:
    tok = _bearer(authorization, x_warden_owner)
    if secrets.compare_digest(tok, OWNER_TOKEN.encode()):
        return "user"
    if secrets.compare_digest(tok, AGENT_TOKEN.encode()):
        return "external-agent"
    raise HTTPException(401, "agent token required (see data/agent_token)")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state", dependencies=[Depends(owner)])
def state():
    return hub.snapshot()


# ---- requests ------------------------------------------------------------
@app.post("/api/tasks", dependencies=[Depends(owner)])
def create_task(body: dict = Body(...)):
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    return hub.submit_text(text)


@app.post("/api/buy", dependencies=[Depends(owner)])
def buy_sku(body: dict = Body(...)):
    """Buy a specific catalog item (e.g. after research). Still goes through policy."""
    return hub.submit_intent(body, "user", source="user")


@app.post("/api/intents")
def external_intent(body: dict = Body(...), who: str = Depends(agent)):
    """Any external agent can ask the hub to buy something. The hub decides,
    and the hub - not the request body - says who the source is."""
    return hub.submit_intent(body, who, source="user" if who == "user" else "agent")


# ---- approvals -------------------------------------------------------------
@app.post("/api/approvals/{aid}/{action}", dependencies=[Depends(owner)])
def decide(aid: int, action: str):
    if action not in ("approve", "reject"):
        raise HTTPException(400)
    return hub.decide(aid, action == "approve")


# ---- policy ------------------------------------------------------------
# ---- handoffs (no-payment mode) --------------------------------------------
@app.post("/api/orders/{oid}/paid", dependencies=[Depends(owner)])
def order_paid(oid: int, body: dict = Body(...)):
    """You paid at the store. Record what was actually charged."""
    try:
        amount = float(body["amount"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "amount required")
    return hub.mark_paid(oid, amount, str(body.get("merchant_order_id") or ""))


@app.post("/api/orders/{oid}/skip", dependencies=[Depends(owner)])
def order_skip(oid: int):
    return hub.skip_handoff(oid)


@app.get("/api/track-record", dependencies=[Depends(agent)])
def track_record():
    return hub.track_record()


@app.get("/api/policy", dependencies=[Depends(agent)])
def get_policy():
    today, month = hub.spend()
    pol = hub.policy
    return {**pol.to_dict(), "spend": {"today": today, "month": month},
            "remaining": {"today": round(max(0, pol.daily_cap - today), 2),
                          "month": round(max(0, pol.monthly_cap - month), 2)}}


@app.put("/api/policy", dependencies=[Depends(owner)])
def put_policy(body: dict = Body(...)):
    return hub.set_policy(body).to_dict()


# ---- monitors ------------------------------------------------------------
@app.post("/api/monitors")
def add_monitor(body: dict = Body(...), who: str = Depends(agent)):
    """Agents may set monitors too: a monitor that buys still goes through policy."""
    merchant = body.get("merchant", "demo-store")
    sku = body.get("sku") or body.get("url")
    a = REGISTRY.get(merchant)
    p = a.get_product(sku) if a and sku else None
    if not p:
        raise HTTPException(404, "product not found / price unreadable")
    action = body.get("action") or ("buy" if a.can_purchase else "notify")
    if action not in ("buy", "notify"):
        raise HTTPException(400, "action must be buy or notify")
    if body.get("max_price") is None and body.get("min_discount_pct") is None:
        raise HTTPException(400, "set max_price or min_discount_pct")
    mid = hub.add_monitor(merchant, sku, p.name, body.get("max_price"), body.get("min_discount_pct"),
                          int(body.get("quantity", 1)), action, actor=who)
    return {"monitor_id": mid, "product": p.to_dict(), "action": action}


@app.get("/api/approvals/{aid}", dependencies=[Depends(agent)])
def approval_status(aid: int):
    return hub.request_status(approval_id=aid)


@app.get("/api/orders/{oid}", dependencies=[Depends(agent)])
def order_status(oid: int):
    return hub.request_status(order_id=oid)


@app.delete("/api/monitors/{mid}", dependencies=[Depends(owner)])
def del_monitor(mid: int):
    hub.cancel_monitor(mid)
    return {"ok": True}


@app.post("/api/monitors/tick", dependencies=[Depends(owner)])
def tick():
    return {"fired": hub.tick_monitors()}


# ---- ledger / data feed -------------------------------------------------
@app.get("/api/ledger", dependencies=[Depends(owner)])
def ledger(limit: int = 200, kind: str | None = None):
    return hub.ledger.list(limit, kind)


@app.get("/api/ledger/verify", dependencies=[Depends(agent)])
def verify():
    return hub.ledger.verify()


@app.get("/api/feed", dependencies=[Depends(agent)])
def feed(since_id: int = 0):
    """Pull-based data feed: poll with the last id you saw."""
    rows = hub.ledger.db.execute("SELECT * FROM events WHERE id>? ORDER BY id ASC LIMIT 500", (since_id,))
    return [{**dict(r), "data": json.loads(r["data"])} for r in rows]


@app.get("/api/export/orders.csv", response_class=PlainTextResponse, dependencies=[Depends(owner)])
def export_orders():
    rows = hub.snapshot()["orders"]
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    return buf.getvalue()


# ---- catalog + demo controls ---------------------------------------------
@app.get("/api/catalog", dependencies=[Depends(agent)])
def catalog(q: str = ""):
    store = REGISTRY["demo-store"]
    items = store.search(q) if q else list(store.products.values())
    return [p.to_dict() for p in items]


@app.post("/api/demo/sale", dependencies=[Depends(owner)])
def demo_sale(body: dict = Body(...)):
    p = REGISTRY["demo-store"].set_sale(body["sku"], float(body.get("pct_off", 25)))
    hub.log("demo_price_change", "demo", sku=p.sku, price=p.price, pct_off=body.get("pct_off", 25))
    fired = hub.tick_monitors()
    return {"product": p.to_dict(), "fired": fired}


@app.post("/api/demo/reset", dependencies=[Depends(owner)])
def demo_reset():
    for p in REGISTRY["demo-store"].products.values():
        p.price = p.list_price
    return {"ok": True}


@app.get("/demo/cart/{sku}", response_class=HTMLResponse)
def demo_cart(sku: str, qty: int = 1):
    """Stand-in for a real store's checkout page in handoff mode."""
    p = REGISTRY["demo-store"].get_product(sku)
    if not p:
        raise HTTPException(404)
    name, total = html.escape(p.name), f"${p.price * qty:,.2f}"
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demo checkout</title><style>body{{margin:0;background:#09090b;color:#ececef;font:14px/1.5 system-ui,sans-serif;
display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}}.c{{max-width:420px;width:100%;
border:1px solid #1f1f25;border-radius:12px;background:#111114;padding:24px}}.m{{color:#77777f;font-size:12.5px}}
.t{{font:500 28px ui-monospace,monospace;margin:12px 0}}</style>
<div class="c"><div class="m">demo-store · checkout</div><h2 style="margin:6px 0 0;font-weight:500">{name} ×{qty}</h2>
<div class="t">{total}</div><p class="m">This is where a real store would ask for your card. Warden prepared the cart
and never saw a payment method. Close this tab and click <b>Mark paid</b> in Warden to record what you were charged.</p></div>"""


def main():
    import uvicorn
    host = os.getenv("WARDEN_HOST", "127.0.0.1")
    port = int(os.getenv("WARDEN_PORT", "8787"))
    print(f"\n  Warden hub (open this link; it carries your owner token)\n"
          f"    http://{host}:{port}/#owner={OWNER_TOKEN}\n\n"
          f"  Agent token for your agents: data/agent_token\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
