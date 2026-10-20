"""The hub: ties requests, policy, approvals, execution, monitors and orders
together, and writes every step to the audit ledger."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import models
from .ledger import Ledger
from .merchants import REGISTRY, Product
from .policy import Policy, PurchaseIntent, evaluate
from .vault import MockVault

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, ts REAL, text TEXT, parsed TEXT,
    parser TEXT, status TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS approvals (id INTEGER PRIMARY KEY, ts REAL, task_id INTEGER,
    intent TEXT, product TEXT, reasons TEXT, status TEXT, decided_ts REAL);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, ts REAL, task_id INTEGER,
    merchant TEXT, requested_merchant TEXT, sku TEXT, item TEXT, qty INTEGER, unit_price REAL,
    total REAL, merchant_order_id TEXT, status TEXT, approved_by TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS monitors (id INTEGER PRIMARY KEY, ts REAL, task_id INTEGER,
    merchant TEXT, sku TEXT, name TEXT, max_price REAL, min_discount_pct REAL, qty INTEGER,
    action TEXT, status TEXT, last_price REAL, history TEXT, checked REAL, triggered_ts REAL);
"""


class Hub:
    def __init__(self, data_dir: str | Path = "data"):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.policy_path = self.dir / "policy.json"
        self.policy = Policy.load(self.policy_path)
        self.ledger = Ledger(self.dir / "ledger.db")
        self.db = sqlite3.connect(self.dir / "state.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.vault = MockVault()
        self.lock = threading.RLock()
        self._stop = threading.Event()

    # ------------------------------------------------------------ helpers --
    def _rows(self, q: str, *a) -> list[dict]:
        return [dict(r) for r in self.db.execute(q, a).fetchall()]

    def _exec(self, q: str, *a) -> int:
        cur = self.db.execute(q, a)
        self.db.commit()
        return cur.lastrowid

    def log(self, kind: str, actor: str = "hub", **data) -> dict:
        return self.ledger.append(kind, data, actor)

    def notify(self, title: str, body: str) -> None:
        self.log("notify", title=title, body=body)
        url = os.getenv("WARDEN_WEBHOOK_URL")  # e.g. https://ntfy.sh/your-topic or a Slack webhook
        if url:
            try:
                req = urllib.request.Request(url, data=json.dumps({"text": f"{title}: {body}"}).encode(),
                                             headers={"Content-Type": "application/json", "Title": title})
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass

    def spend(self) -> tuple[float, float]:
        now = datetime.now()
        day0 = datetime(now.year, now.month, now.day).timestamp()
        mon0 = datetime(now.year, now.month, 1).timestamp()
        q = "SELECT COALESCE(SUM(total),0) s FROM orders WHERE status!='cancelled' AND ts>=?"
        return (round(self.db.execute(q, (day0,)).fetchone()["s"], 2), round(self.db.execute(q, (mon0,)).fetchone()["s"], 2))

    def set_policy(self, updates: dict, actor: str = "user") -> Policy:
        with self.lock:
            before = self.policy.to_dict()
            for k, v in updates.items():
                if k in Policy.__dataclass_fields__:
                    setattr(self.policy, k, v)
            self.policy.risk_level = max(0, min(4, int(self.policy.risk_level)))
            self.policy.save(self.policy_path)
            changed = {k: [before[k], v] for k, v in updates.items() if k in before and before[k] != v}
            self.log("policy_changed", actor, changes=changed)
            return self.policy

    # ------------------------------------------------- resolve merchants ---
    def _adapter_for(self, requested: str | None):
        if requested and requested in REGISTRY and REGISTRY[requested].can_purchase:
            return REGISTRY[requested], None
        note = None
        if requested and requested not in REGISTRY:
            note = f"'{requested}' isn't connected yet - simulated with demo-store."
        return REGISTRY["demo-store"], note

    @staticmethod
    def _pick(products: list[Product], max_price: float | None) -> Product | None:
        ok = [p for p in products if p.in_stock and (max_price is None or p.price <= max_price)]
        return ok[0] if ok else None

    # ----------------------------------------------------- entry points ----
    def submit_text(self, text: str, actor: str = "user") -> dict:
        parsed, parser = models.parse_intent(text)
        tid = self._exec("INSERT INTO tasks (ts,text,parsed,parser,status,result) VALUES (?,?,?,?,?,?)",
                         time.time(), text, json.dumps(parsed), parser, "received", "")
        self.log("task_received", actor, task_id=tid, text=text, parsed=parsed, parser=parser)
        handler = {"buy": self._do_buy, "monitor": self._do_monitor, "research": self._do_research}
        result = handler.get(parsed["type"], self._do_buy)(tid, parsed)
        status = result.get("status", "done")
        self._exec("UPDATE tasks SET status=?, result=? WHERE id=?", status, json.dumps(result), tid)
        return {"task_id": tid, "parsed": parsed, "parser": parser, **result}

    def submit_intent(self, d: dict, agent_id: str) -> dict:
        """For external agents (your own local agent, scripts, etc.) to request a purchase.
        The hub - not the agent - looks up the live price and decides."""
        adapter, note = self._adapter_for(d.get("merchant"))
        p = adapter.get_product(d.get("sku", ""))
        if not p:
            self.log("intent_rejected", agent_id, reason="unknown sku", intent=d)
            return {"status": "error", "error": "unknown sku"}
        self.log("intent_received", agent_id, intent=d)
        return self._propose(p, int(d.get("quantity", 1)), d.get("source", "local"), None,
                             d.get("merchant"), actor=agent_id, note=note)

    # ------------------------------------------------------- handlers ------
    def _do_buy(self, tid: int, parsed: dict) -> dict:
        adapter, note = self._adapter_for(parsed.get("merchant"))
        found = adapter.search(parsed["query"])
        p = self._pick(found, parsed.get("max_price"))
        if not p:
            self.log("no_match", task_id=tid, query=parsed["query"], candidates=len(found))
            return {"status": "no_match", "message": f"No in-policy match for '{parsed['query']}'.",
                    "candidates": [x.to_dict() for x in found[:5]]}
        return self._propose(p, parsed.get("quantity", 1), "local", tid, parsed.get("merchant"), note=note)

    def _do_monitor(self, tid: int, parsed: dict) -> dict:
        adapter, note = self._adapter_for(parsed.get("merchant"))
        found = adapter.search(parsed["query"])
        if not found:
            return {"status": "no_match", "message": f"Couldn't find '{parsed['query']}' to watch."}
        p = found[0]
        mid = self.add_monitor(adapter.name, p.sku, p.name, parsed.get("max_price"),
                               parsed.get("min_discount_pct"), parsed.get("quantity", 1),
                               parsed.get("action_on_trigger", "buy"), tid)
        return {"status": "monitoring", "monitor_id": mid, "product": p.to_dict(), "note": note,
                "message": f"Watching {p.name} (now ${p.price:.2f})."}

    def _do_research(self, tid: int, parsed: dict) -> dict:
        task = self.db.execute("SELECT text FROM tasks WHERE id=?", (tid,)).fetchone()["text"]
        if not self.policy.frontier_research_allowed:
            answer, sent, used = "(frontier research disabled by policy)", "", "none"
        else:
            answer, sent = models.frontier_research(task, self.policy.frontier_redact_pii)
            used = models.FRONTIER_PROVIDER if models.frontier_status()["configured"] else "local-fallback"
        self.log("research", "frontier" if used not in ("none", "local-fallback") else "local",
                 task_id=tid, sent=sent, model=used, answer_preview=answer[:400])
        q = parsed["query"]
        for line in answer.splitlines():
            if line.upper().startswith("SEARCH:"):
                q = line.split(":", 1)[1].strip()
        adapter, _ = self._adapter_for(parsed.get("merchant"))
        matches = [p.to_dict() for p in adapter.search(q)[:5]] or [p.to_dict() for p in adapter.search(parsed["query"])[:5]]
        return {"status": "researched", "answer": answer, "sent_to_frontier": sent,
                "matches": matches, "message": "Research done. Nothing was bought - pick a match to buy."}

    # ------------------------------------------------ propose / execute ----
    def _propose(self, p: Product, qty: int, source: str, tid: int | None,
                 requested: str | None, actor: str = "agent", note: str | None = None) -> dict:
        with self.lock:
            intent = PurchaseIntent(p.merchant, p.name, p.price, qty, p.category, source)
            today, month = self.spend()
            dec = evaluate(self.policy, intent, today, month)
            self.log("policy_decision", actor, task_id=tid, intent=intent.__dict__,
                     total=intent.total, decision=dec.to_dict(), risk_level=self.policy.risk_level)
            base = {"product": p.to_dict(), "total": intent.total, "decision": dec.to_dict(), "note": note}
            if dec.outcome == "deny":
                return {**base, "status": "denied", "message": "Blocked by policy: " + " ".join(dec.reasons)}
            if dec.outcome == "require_approval":
                aid = self._exec(
                    "INSERT INTO approvals (ts,task_id,intent,product,reasons,status) VALUES (?,?,?,?,?,?)",
                    time.time(), tid, json.dumps({**intent.__dict__, "requested_merchant": requested}),
                    json.dumps(p.to_dict()), json.dumps(dec.reasons), "pending")
                self.notify("Approval needed", f"{p.name} x{qty} for ${intent.total:.2f}")
                return {**base, "status": "awaiting_approval", "approval_id": aid,
                        "message": f"Waiting for your OK: {p.name} for ${intent.total:.2f}."}
            order = self._execute(p, qty, tid, requested, approved_by="policy:auto")
            return {**base, **order}

    def _execute(self, p: Product, qty: int, tid: int | None, requested: str | None, approved_by: str) -> dict:
        adapter = REGISTRY[p.merchant]
        live = adapter.get_product(p.sku)
        if not live or live.price > p.price * 1.02:  # price moved up since the decision: stop
            self.log("execution_aborted", task_id=tid, reason="price changed", was=p.price,
                     now=live.price if live else None)
            return {"status": "aborted", "message": "Price changed since approval - not purchased."}
        total = round(live.price * qty, 2)
        tok = self.vault.mint(adapter.name, total)
        self.log("payment_token_minted", "vault", merchant=adapter.name, limit=tok.max_amount, task_id=tid)
        res = adapter.place_order(live.sku, qty, tok.token)
        if res.ok:
            ok, why = self.vault.charge(tok.token, adapter.name, total)
            if not ok:
                res.ok, res.error = False, f"payment refused: {why}"
        if not res.ok:
            self.log("order_failed", task_id=tid, sku=live.sku, error=res.error)
            return {"status": "failed", "message": res.error}
        oid = self._exec(
            "INSERT INTO orders (ts,task_id,merchant,requested_merchant,sku,item,qty,unit_price,total,"
            "merchant_order_id,status,approved_by,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            time.time(), tid, adapter.name, requested, live.sku, live.name, qty, live.price, total,
            res.merchant_order_id, res.status, approved_by, time.time())
        self.log("order_placed", "executor", order_id=oid, merchant=adapter.name, item=live.name,
                 qty=qty, total=total, merchant_order_id=res.merchant_order_id, approved_by=approved_by)
        self.notify("Order placed", f"{live.name} x{qty} - ${total:.2f} ({res.merchant_order_id})")
        return {"status": "ordered", "order_id": oid, "merchant_order_id": res.merchant_order_id,
                "message": f"Ordered {live.name} for ${total:.2f}."}

    # ------------------------------------------------------- approvals -----
    def decide(self, aid: int, approve: bool, actor: str = "user") -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
            if not row or row["status"] != "pending":
                return {"status": "error", "message": "Not pending."}
            self._exec("UPDATE approvals SET status=?, decided_ts=? WHERE id=?",
                       "approved" if approve else "rejected", time.time(), aid)
            self.log("approval_" + ("granted" if approve else "rejected"), actor, approval_id=aid)
            if not approve:
                return {"status": "rejected"}
            it = json.loads(row["intent"])
            p = Product(**{k: v for k, v in json.loads(row["product"]).items() if k != "discount_pct"})
            # A human OK overrides the risk dial, never the hard limits.
            today, month = self.spend()
            dec = evaluate(self.policy, PurchaseIntent(p.merchant, p.name, p.price, it["quantity"],
                                                       p.category, it["source"]), today, month)
            if dec.outcome == "deny":
                self.log("execution_aborted", reason="hard limit at approval time", decision=dec.to_dict())
                return {"status": "denied", "message": " ".join(dec.reasons)}
            return self._execute(p, it["quantity"], row["task_id"], it.get("requested_merchant"),
                                 approved_by=actor)

    # -------------------------------------------------------- monitors -----
    def add_monitor(self, merchant, sku, name, max_price, min_discount_pct, qty=1, action="buy", tid=None) -> int:
        mid = self._exec(
            "INSERT INTO monitors (ts,task_id,merchant,sku,name,max_price,min_discount_pct,qty,action,"
            "status,history) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            time.time(), tid, merchant, sku, name, max_price, min_discount_pct, qty, action, "active", "[]")
        self.log("monitor_created", task_id=tid, monitor_id=mid, merchant=merchant, sku=sku, name=name,
                 max_price=max_price, min_discount_pct=min_discount_pct, action=action)
        return mid

    def cancel_monitor(self, mid: int) -> None:
        self._exec("UPDATE monitors SET status='cancelled' WHERE id=?", mid)
        self.log("monitor_cancelled", "user", monitor_id=mid)

    def tick_monitors(self) -> list[dict]:
        fired = []
        for m in self._rows("SELECT * FROM monitors WHERE status='active'"):
            adapter = REGISTRY.get(m["merchant"])
            p = adapter.get_product(m["sku"]) if adapter else None
            if not p:
                continue
            hist = json.loads(m["history"])[-49:] + [[time.time(), p.price]]
            self._exec("UPDATE monitors SET last_price=?, history=?, checked=? WHERE id=?",
                       p.price, json.dumps(hist), time.time(), m["id"])
            hit = True
            if m["max_price"] is not None and p.price > m["max_price"]:
                hit = False
            if m["min_discount_pct"] is not None and p.discount_pct < m["min_discount_pct"]:
                hit = False
            if m["max_price"] is None and m["min_discount_pct"] is None:
                hit = False
            if not hit:
                continue
            self._exec("UPDATE monitors SET status='triggered', triggered_ts=? WHERE id=?", time.time(), m["id"])
            self.log("monitor_triggered", "monitor", monitor_id=m["id"], price=p.price, discount=p.discount_pct)
            if m["action"] == "notify" or not adapter.can_purchase:
                self.notify("Price alert", f"{p.name} is ${p.price:.2f} ({p.discount_pct}% off)")
                fired.append({"monitor_id": m["id"], "status": "notified"})
            else:
                fired.append({"monitor_id": m["id"], **self._propose(p, m["qty"], "local", m["task_id"], m["merchant"], actor="monitor")})
        return fired

    # ---------------------------------------------------------- orders -----
    def refresh_orders(self) -> None:
        for o in self._rows("SELECT * FROM orders WHERE status NOT IN ('delivered','cancelled')"):
            a = REGISTRY.get(o["merchant"])
            new = a.order_status(o["merchant_order_id"]) if a else o["status"]
            if new != o["status"] and new != "unknown":
                self._exec("UPDATE orders SET status=?, updated=? WHERE id=?", new, time.time(), o["id"])
                self.log("order_status", "merchant", order_id=o["id"], status=new, was=o["status"])

    # ------------------------------------------------------ background -----
    def start_background(self, interval: float = 15.0, jitter_demo: bool = True) -> None:
        def loop():
            while not self._stop.wait(interval):
                try:
                    if jitter_demo:
                        REGISTRY["demo-store"].jitter()
                    self.tick_monitors()
                    self.refresh_orders()
                except Exception as e:
                    self.log("background_error", error=str(e))
        threading.Thread(target=loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------- snapshot ------
    def snapshot(self) -> dict:
        today, month = self.spend()
        return {
            "policy": self.policy.to_dict(),
            "spend": {"today": today, "month": month},
            "approvals": self._rows("SELECT * FROM approvals ORDER BY id DESC LIMIT 50"),
            "orders": self._rows("SELECT * FROM orders ORDER BY id DESC LIMIT 100"),
            "monitors": self._rows("SELECT * FROM monitors ORDER BY id DESC LIMIT 100"),
            "tasks": self._rows("SELECT * FROM tasks ORDER BY id DESC LIMIT 30"),
            "models": {"local": models.local_status(), "frontier": models.frontier_status()},
            "ledger_ok": self.ledger.verify(),
        }
