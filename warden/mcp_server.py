"""MCP server: plug Warden into Claude Desktop, Claude Code, Cursor, OpenClaw...

The MCP client's model becomes "the agent". It can look up products, propose
purchases, set price watches and follow up on its requests. It holds only the
agent token, so it can never approve, pay, or change your policy - those stay
with you in the dashboard.

Talks to a running hub over HTTP:

    ./run.sh                      # terminal 1: the hub
    ./mcp.sh                      # what your MCP client launches (stdio)

Env: WARDEN_URL (default http://127.0.0.1:8787), WARDEN_AGENT_TOKEN or
WARDEN_DATA (to read data/agent_token).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

HUB = os.getenv("WARDEN_URL", "http://127.0.0.1:8787").rstrip("/")
DATA = Path(os.getenv("WARDEN_DATA", Path(__file__).resolve().parent.parent / "data"))

INSTRUCTIONS = """Warden is the user's local purchase hub. You never handle payment details:
do not ask for or accept card numbers, bank logins or passwords.

To buy something from a connected store: search_products, then request_purchase(sku=...).
For any other website: request_purchase(url=<product page>, price=<price you see>,
name=<product name>). Warden reads the price itself where it can; otherwise your price
is marked unverified and the user must approve. The user always pays on that site.
Always give a short reason the user will read. Warden replies with one of:
- handed_off: the cart is ready and the USER pays at the store. Tell them it's in
  Warden's "Ready for you to pay" list. Do not try to pay yourself.
- awaiting_approval: the user must approve in Warden first. Follow up later with
  check_request(approval_id=...).
- ordered: Warden paid (only at stores the user trusts it with).
- denied: a hard limit blocked it. Explain the reason; don't retry the same request.
Call get_policy first if you're unsure what is allowed or how much budget is left.
For "buy when it's on sale" requests use watch_price."""


class HubError(ToolError):
    """Shown to the model as the tool's error message, so it can explain or correct."""


def _token() -> str:
    tok = os.getenv("WARDEN_AGENT_TOKEN")
    if not tok:
        try:
            tok = (DATA / "agent_token").read_text().strip()
        except OSError:
            raise HubError(f"No agent token: set WARDEN_AGENT_TOKEN or start the hub once so "
                           f"{DATA / 'agent_token'} exists.") from None
    try:
        if tok == (DATA / "owner_token").read_text().strip():
            raise HubError("WARDEN_AGENT_TOKEN is your owner token. Give the MCP server the agent token.")
    except OSError:
        pass
    return tok


def _call(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        HUB + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", e.reason)
        except Exception:
            detail = e.reason
        raise HubError(f"Warden hub refused ({e.code}): {detail}") from None
    except urllib.error.URLError as e:
        raise HubError(f"Warden hub isn't reachable at {HUB} ({e.reason}). Start it with ./run.sh.") from None


READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

mcp = _Server("warden", instructions=INSTRUCTIONS)


@mcp.tool(annotations=READ)
def get_policy() -> dict:
    """What you're allowed to do right now: risk level, payment mode (handoff = the user
    pays), per-purchase max, budget left today and this month, blocked merchants and categories."""
    p = _call("GET", "/api/policy")
    keep = ("risk_level", "risk_name", "risk_description", "payment_mode", "pay_merchants",
            "max_per_transaction", "auto_approve_under", "daily_cap", "monthly_cap", "remaining",
            "allowed_merchants", "blocked_merchants", "blocked_categories", "require_approval_categories")
    return {k: p[k] for k in keep if k in p}


@mcp.tool(annotations=READ)
def search_products(query: str) -> list[dict]:
    """Search the stores Warden can buy from. Returns sku, name, merchant, live price,
    list price, discount and category. Use the sku with request_purchase."""
    return _call("GET", "/api/catalog?" + urllib.parse.urlencode({"q": query}))[:10]


@mcp.tool(annotations=WRITE)
def request_purchase(reason: str, sku: str | None = None, url: str | None = None,
                     price: float | None = None, name: str | None = None,
                     quantity: int = 1, merchant: str = "demo-store") -> dict:
    """Ask Warden to buy a product: either `sku` from search_products, or `url` of a product
    page on any website (with the `price` and `name` you see there). Warden reads the live
    price itself and applies the user's policy. `reason` is shown to the user next to the
    request; say in one sentence why this item fits what they asked for. You never pay and
    never see payment details."""
    if url:
        body = {"url": url, "price": price, "name": name, "quantity": quantity, "reason": reason}
    elif sku:
        body = {"merchant": merchant, "sku": sku, "quantity": quantity, "reason": reason}
    else:
        raise HubError("Pass sku (from search_products) or url (a product page).")
    r = _call("POST", "/api/intents", body)
    if r.get("status") == "error":
        raise HubError(r.get("message") or r.get("error"))
    out = {k: r[k] for k in ("status", "message", "total", "order_id", "approval_id",
                             "cart_url", "expected_total", "merchant_order_id", "note") if r.get(k) is not None}
    if r.get("decision"):
        out["policy_reasons"] = r["decision"]["reasons"]
    out["next_step"] = {
        "handed_off": "The user pays from Warden's 'Ready for you to pay' list. You're done.",
        "awaiting_approval": "The user decides in Warden. Check later with check_request(approval_id).",
        "ordered": "Warden paid. Track it with check_request(order_id).",
        "denied": "Blocked by a hard limit. Tell the user why; don't retry as-is.",
    }.get(r.get("status"), "See message.")
    return out


@mcp.tool(annotations=READ)
def check_request(approval_id: int | None = None, order_id: int | None = None) -> dict:
    """Follow up on a request from request_purchase: pass the approval_id or order_id it
    returned. Shows whether it's waiting, approved, handed off, paid (and how much), or shipped."""
    if approval_id is not None:
        return _call("GET", f"/api/approvals/{int(approval_id)}")
    if order_id is not None:
        return _call("GET", f"/api/orders/{int(order_id)}")
    raise HubError("Pass approval_id or order_id.")


@mcp.tool(annotations=WRITE)
def watch_price(sku: str, max_price: float | None = None, min_discount_pct: float | None = None,
                action: str = "notify", quantity: int = 1, merchant: str = "demo-store") -> dict:
    """Watch a product and act when it hits a price (max_price) or a discount off list
    (min_discount_pct). action="notify" alerts the user; action="buy" makes a purchase
    request that still goes through policy (on websites the user then pays). For any
    product page on the web, use merchant="web" and pass the page URL as sku."""
    return _call("POST", "/api/monitors", {"merchant": merchant, "sku": sku, "max_price": max_price,
                                           "min_discount_pct": min_discount_pct, "action": action,
                                           "quantity": quantity})


@mcp.tool(annotations=READ)
def track_record() -> dict:
    """How often the user paid what you (and other agents) proposed, overall and per store.
    Stores where they consistently pay unchanged can be switched to Warden paying."""
    t = _call("GET", "/api/track-record")
    return {"overall": t["overall"], "merchants": t["merchants"]}


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
