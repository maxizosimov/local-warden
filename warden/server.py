"""HTTP API + dashboard. Binds to localhost by default - your data stays on your machine."""
from __future__ import annotations

import csv
import io
import html
import json
import threading
import os
import re
import secrets
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from . import local_models, models
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
    hub.start_background(float(os.getenv("WARDEN_TICK_SECONDS", "15")))
    hub.log("hub_started", "hub", risk_level=hub.policy.risk_level)
    if os.getenv("WARDEN_START_OLLAMA", "1") == "1" and local_models.engine().get("can_start"):
        threading.Thread(target=_start_ollama, args=("hub",), daemon=True).start()
    yield
    hub.stop()


app = FastAPI(title="Warden", version="0.2.0", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"
# Hostnames the hub answers to. Add yours when hosting it (Render sets RENDER_EXTERNAL_HOSTNAME).
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", os.getenv("WARDEN_HOST", "127.0.0.1"),
               os.getenv("RENDER_EXTERNAL_HOSTNAME", "localhost"),
               *filter(None, os.getenv("WARDEN_ALLOWED_HOSTS", "").split(","))}


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
# ---- local model: pick, download, test -------------------------------------
def _model_name(body: dict) -> str:
    name = str(body.get("name") or "").strip()
    if not local_models.NAME_RE.match(name):
        raise HTTPException(400, "invalid model name")
    return name


@app.get("/api/models", dependencies=[Depends(owner)])
def models_overview():
    return local_models.overview()


def _start_ollama(actor: str) -> dict:
    """Start an installed Ollama that isn't running. Warden does this itself at launch."""
    try:
        eng = local_models.start_ollama()
    except RuntimeError as e:
        hub.log("ollama_start_failed", actor, error=str(e))
        raise
    hub.log("ollama_started", actor, version=eng.get("version", ""))
    return eng


@app.post("/api/models/start", dependencies=[Depends(owner)])
def models_start():
    if local_models.engine()["running"]:
        return {"status": "running"}
    try:
        eng = _start_ollama("user")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"status": "running", "version": eng.get("version", "")}


@app.post("/api/models/stop", dependencies=[Depends(owner)])
def models_stop():
    r = local_models.stop_ollama()
    hub.log("ollama_stopped" if r["stopped"] else "models_unloaded", "user", unloaded=r["unloaded"])
    return {**r, "message": "Ollama stopped. Rules do its jobs until you start it." if r["stopped"] else
            "Unloaded from memory. Ollama was started outside Warden, so quit it from its menu bar to stop it."}


@app.post("/api/models/pull", dependencies=[Depends(owner)])
def models_pull(body: dict = Body(...)):
    if local_models.engine()["kind"] != "ollama":
        raise HTTPException(409, "Downloads need Ollama running (ollama.com/download).")
    name = _model_name(body)

    def done(n):  # start using it if the current model isn't downloaded
        if not local_models.overview()["active_installed"]:
            hub.set_policy({"local_model": n}, actor="hub")
    local_models.start_pull(name, done)
    hub.log("model_download_started", "user", model=name)
    return {"status": "downloading", "name": name}


@app.post("/api/models/use", dependencies=[Depends(owner)])
def models_use(body: dict = Body(...)):
    return {"active": hub.set_policy({"local_model": _model_name(body)}).local_model}


@app.post("/api/models/delete", dependencies=[Depends(owner)])
def models_delete(body: dict = Body(...)):
    name = _model_name(body)
    try:
        local_models.delete(name)
    except Exception as e:
        raise HTTPException(502, f"Couldn't delete {name}: {e}")
    hub.log("model_deleted", "user", model=name)
    return {"ok": True}


@app.post("/api/models/test", dependencies=[Depends(owner)])
def models_test():
    chairs = [REGISTRY["demo-store"].get_product(s) for s in ("CHR-200", "CHR-100")]
    results = local_models.test_jobs(chairs)
    hub.log("model_tested", "user", model=models.LOCAL_MODEL,
            passed=sum(r["ok"] for r in results), of=len(results))
    return {"model": models.LOCAL_MODEL, "results": results}


# ---- accounts: store logins and payment methods (masked only) ---------------
# Warden keeps who and which, never the secret: a label, a login name, the last 4
# digits. Passwords and full card or account numbers are never sent here, so a
# leaked data folder (or an agent) can't use them. Real payment goes through
# single-use virtual cards (see vault.py), not these.
WALLET = DATA / "wallet.json"
WALLET_KINDS = {"login": ("store", "login"), "card": ("label", "last4"), "bank": ("label", "last4")}
DEMO_WALLET = [
    {"kind": "login", "store": "demo-store", "login": "demo.shopper@example.com"},
    {"kind": "card", "label": "Demo Visa", "last4": "4242"},
    {"kind": "bank", "label": "Demo Checking", "last4": "6789"},
]


def _wallet() -> list[dict]:
    if not WALLET.exists():
        _save_wallet([{**w, "id": secrets.token_hex(4), "demo": True} for w in DEMO_WALLET])
    return json.loads(WALLET.read_text())


def _save_wallet(items: list[dict]) -> None:
    WALLET.write_text(json.dumps(items, indent=1))
    WALLET.chmod(0o600)


@app.get("/api/wallet", dependencies=[Depends(owner)])
def wallet():
    return _wallet()


@app.post("/api/wallet", dependencies=[Depends(owner)])
def wallet_add(body: dict = Body(...)):
    kind = body.get("kind")
    if kind not in WALLET_KINDS:
        raise HTTPException(400, "kind must be login, card or bank")
    item = {"kind": kind, "id": secrets.token_hex(4)}
    for k in WALLET_KINDS[kind]:
        v = str(body.get(k) or "").strip()
        if not v or len(v) > 80:
            raise HTTPException(400, f"{k} required")
        item[k] = v
    if "last4" in item and not re.fullmatch(r"\d{4}", item["last4"]):
        raise HTTPException(400, "last4 must be the last 4 digits only")
    items = _wallet() + [item]
    _save_wallet(items)
    hub.log("account_added", "user", type=kind, name=item.get("store") or item.get("label"))
    return item


@app.delete("/api/wallet/{wid}", dependencies=[Depends(owner)])
def wallet_remove(wid: str):
    items = _wallet()
    gone = [w for w in items if w["id"] == wid]
    if not gone:
        raise HTTPException(404)
    _save_wallet([w for w in items if w["id"] != wid])
    hub.log("account_removed", "user", type=gone[0]["kind"], name=gone[0].get("store") or gone[0].get("label"))
    return {"ok": True}


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


@app.post("/api/demo/clear", dependencies=[Depends(owner)])
def demo_clear():
    """Start the guided demo from a clean board. History stays in the ledger."""
    for p in REGISTRY["demo-store"].products.values():
        p.price = p.list_price
    for a in hub._rows("SELECT id FROM approvals WHERE status='pending'"):
        hub.decide(a["id"], False, actor="demo")
    for o in hub._rows("SELECT id FROM orders WHERE status='handed_off'"):
        hub.skip_handoff(o["id"], actor="demo")
    for m in hub._rows("SELECT id FROM monitors WHERE status='active'"):
        hub.cancel_monitor(m["id"])
    hub.set_policy({"risk_level": 1, "pay_merchants": []}, actor="demo")
    hub.log("demo_cleared", "demo")
    return {"ok": True}


@app.get("/demo/cart/{sku}", response_class=HTMLResponse)
def demo_cart(sku: str, qty: int = 1):
    """Stand-in for a real store's checkout in handoff mode. The card on it is fake and
    never leaves this page: paying only tells Warden the amount, like "Mark paid"."""
    p = REGISTRY["demo-store"].get_product(sku)
    if not p:
        raise HTTPException(404)
    qty = max(1, min(qty, 99))
    name, total = html.escape(p.name), round(p.price * qty, 2)
    return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demo checkout</title><style>body{{margin:0;background:#09090b;color:#ececef;font:14px/1.5 system-ui,sans-serif;
display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}}.c{{max-width:420px;width:100%;
border:1px solid #1f1f25;border-radius:12px;background:#111114;padding:24px;box-sizing:border-box}}
.m{{color:#77777f;font-size:12.5px}}.t{{font:500 28px ui-monospace,monospace;margin:8px 0 16px}}
.sim{{background:#2a2108;color:#f5c451;border-radius:6px;padding:6px 10px;font-size:12.5px;margin-bottom:14px}}
label{{display:block;color:#77777f;font-size:12px;margin:10px 0 4px}}input{{width:100%;box-sizing:border-box;
background:#09090b;border:1px solid #2a2a31;border-radius:8px;color:#ececef;padding:9px 10px;font:14px ui-monospace,monospace}}
.row{{display:flex;gap:10px}}.row>div{{flex:1}}button{{width:100%;margin-top:18px;padding:11px;border:0;border-radius:8px;
background:#ececef;color:#09090b;font:600 14px system-ui;cursor:pointer}}button:disabled{{opacity:.5;cursor:default}}
#out{{margin-top:14px}}a{{color:#8ab4ff}}</style>
<div class="c"><div class="sim">Simulated store. The card below is fake and nothing is charged.</div>
<div class="m">demo-store · checkout</div><h2 style="margin:6px 0 0;font-weight:500">{name} ×{qty}</h2>
<div class="t">${total:,.2f}</div>
<form id="f"><label>Name on card</label><input value="Demo Shopper" autocomplete="off">
<label>Card number</label><input value="4242 4242 4242 4242" autocomplete="off">
<div class="row"><div><label>Expires</label><input value="12/30" autocomplete="off"></div>
<div><label>CVC</label><input value="123" autocomplete="off"></div></div>
<label>Ship to</label><input value="1 Demo Way, Springfield" autocomplete="off">
<button id="pay">Pay ${total:,.2f}</button></form>
<p class="m">Your agent and Warden never see this page's card details. Warden only records what you paid.</p>
<div id="out" class="m"></div></div>
<script>
const SKU = {json.dumps(sku)}, TOTAL = {total}, out = document.getElementById("out");
let owner = ""; try {{ owner = localStorage.getItem("warden_owner") || ""; }} catch (e) {{}}
const api = (path, body) => fetch(path, {{method: body ? "POST" : "GET",
  headers: {{"X-Warden-Owner": owner, "Content-Type": "application/json"}},
  body: body ? JSON.stringify(body) : undefined}}).then(r => r.ok ? r.json() : Promise.reject(r.status));
document.getElementById("f").onsubmit = async (e) => {{
  e.preventDefault(); const btn = document.getElementById("pay"); btn.disabled = true; btn.textContent = "Paying…";
  try {{
    const s = await api("/api/state");
    const o = s.orders.find((o) => o.status === "handed_off" && o.sku === SKU);
    if (!o) throw "This cart isn't waiting for payment in Warden.";
    const r = await api(`/api/orders/${{o.id}}/paid`, {{amount: TOTAL}});
    if (r.status === "error") throw r.message;
    btn.textContent = "Paid (simulated)";
    out.innerHTML = `Order <b>${{r.merchant_order_id}}</b> placed. Warden recorded it: ${{r.message}} <a href="/">Back to Warden</a>`;
  }} catch (err) {{
    btn.disabled = false; btn.textContent = "Pay ${total:,.2f}";
    out.textContent = typeof err === "string" ? err : "Open Warden first so this page can tell it you paid.";
  }}
}};
</script>"""


def main():
    import uvicorn
    host = os.getenv("WARDEN_HOST", "127.0.0.1")
    port = int(os.getenv("WARDEN_PORT") or os.getenv("PORT") or "8787")  # hosts like Render set PORT
    shown = "localhost" if host == "0.0.0.0" else host
    print(f"\n  Warden hub (open this link; it carries your owner token)\n"
          f"    http://{shown}:{port}/#owner={OWNER_TOKEN}\n\n"
          f"  Agent token for your agents: data/agent_token\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
