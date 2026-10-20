import pytest

from warden.merchants import REGISTRY
from warden.models import redact, rule_parse
from warden.policy import Policy, PurchaseIntent, evaluate


# ---- parsing -------------------------------------------------------------
def test_parse_buy():
    d = rule_parse("buy a chair from staples")
    assert d["type"] == "buy" and d["merchant"] == "staples" and "chair" in d["query"]


def test_parse_monitor_discount():
    d = rule_parse("buy the propane grill when it's 20% off")
    assert d["type"] == "monitor" and d["min_discount_pct"] == 20 and "grill" in d["query"]


def test_parse_monitor_notify_price():
    d = rule_parse("tell me when the standing desk drops below $200")
    assert d["type"] == "monitor" and d["action_on_trigger"] == "notify" and d["max_price"] == 200


def test_parse_research():
    assert rule_parse("find me the best office chair under 200")["type"] == "research"


def test_redact():
    s = redact("ship to 42 Main St 94110, card 4111 1111 1111 1111, me@x.com")
    assert "4111" not in s and "me@x.com" not in s and "Main St" not in s


# ---- policy --------------------------------------------------------------
def test_hard_limits_beat_risk_level():
    pol = Policy(risk_level=4)
    assert evaluate(pol, PurchaseIntent("demo-store", "x", 500), 0, 0).outcome == "deny"
    assert evaluate(pol, PurchaseIntent("demo-store", "gc", 20, category="gift_cards"), 0, 0).outcome == "deny"
    assert evaluate(pol, PurchaseIntent("demo-store", "x", 20, source="frontier"), 0, 0).outcome == "deny"
    assert evaluate(pol, PurchaseIntent("demo-store", "x", 50), 180, 0).outcome == "deny"  # daily cap


def test_risk_dial():
    i = PurchaseIntent("demo-store", "pens", 14.49)
    big = PurchaseIntent("demo-store", "chair", 189.99)
    other = PurchaseIntent("other-store", "pens", 14.49)
    assert evaluate(Policy(risk_level=0), i, 0, 0).outcome == "deny"
    assert evaluate(Policy(risk_level=1), i, 0, 0).outcome == "require_approval"
    assert evaluate(Policy(risk_level=2), i, 0, 0).outcome == "allow"
    assert evaluate(Policy(risk_level=2), other, 0, 0).outcome == "require_approval"
    assert evaluate(Policy(risk_level=3), other, 0, 0).outcome == "allow"
    assert evaluate(Policy(risk_level=3), big, 0, 0).outcome == "require_approval"
    assert evaluate(Policy(risk_level=4), big, 0, 0).outcome == "allow"


# ---- end to end ------------------------------------------------------------
def test_buy_chair_needs_approval_then_orders(hub):
    hub.set_policy({"pay_merchants": ["demo-store"]})
    r = hub.submit_text("buy a chair from staples")
    assert r["status"] == "awaiting_approval"
    o = hub.decide(r["approval_id"], True)
    assert o["status"] == "ordered"
    assert hub.snapshot()["orders"][0]["requested_merchant"] == "staples"


def test_auto_buy_small_at_guarded(hub):
    hub.set_policy({"risk_level": 2, "pay_merchants": ["demo-store"]})
    assert hub.submit_text("buy gel pens")["status"] == "ordered"


def test_grill_monitor_buys_on_sale(hub):
    hub.set_policy({"risk_level": 4, "max_per_transaction": 400, "daily_cap": 500, "monthly_cap": 1000,
                    "pay_merchants": ["demo-store"]})
    r = hub.submit_text("buy the propane grill when it's 25% off")
    assert r["status"] == "monitoring" and r["product"]["sku"] == "GRL-400"
    assert hub.tick_monitors() == []
    REGISTRY["demo-store"].set_sale("GRL-400", 30)
    fired = hub.tick_monitors()
    assert fired and fired[0]["status"] == "ordered"


def test_price_rise_aborts(hub):
    r = hub.submit_text("buy basic task chair")
    REGISTRY["demo-store"].products["CHR-200"].price = 99.0
    assert hub.decide(r["approval_id"], True)["status"] == "aborted"


def test_ledger_tamper_detected(hub):
    hub.submit_text("buy gel pens")
    assert hub.ledger.verify()["ok"]
    with pytest.raises(Exception):
        hub.ledger.db.execute("UPDATE events SET data='{}' WHERE id=1")
    # bypass triggers to simulate an attacker editing the file
    hub.ledger.db.execute("DROP TRIGGER no_update")
    hub.ledger.db.execute("UPDATE events SET data='{}' WHERE id=1")
    assert hub.ledger.verify() == {"ok": False, "broken_at": 1, "reason": "content altered", "checked": 1}


def test_vault_token_locked():
    from warden.vault import MockVault
    v = MockVault()
    t = v.mint("demo-store", 10)
    assert not v.charge(t.token, "evil-store", 10)[0]
    assert not v.charge(t.token, "demo-store", 50)[0]
    assert v.charge(t.token, "demo-store", 10)[0]
    assert not v.charge(t.token, "demo-store", 10)[0]  # single use


def test_address_not_quantity():
    d = rule_parse("find me the best office chair under 200, ship to 12 Oak St 94110")
    assert d["quantity"] == 1 and d["query"] == "office chair" and d["max_price"] == 200


def test_explicit_quantity():
    assert rule_parse("buy 3 gel pens")["quantity"] == 3


def test_search_strongest_only():
    assert all("Chair" in p.name for p in REGISTRY["demo-store"].search("office chair"))
