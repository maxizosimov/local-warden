"""No-payment (handoff) mode: the agent prepares the purchase, the human pays."""
from warden.hub import TRUST_MIN_PROPOSALS


def kinds(hub):
    return [e["kind"] for e in hub.ledger.list()]


def test_default_is_handoff_and_mints_no_token(hub):
    hub.set_policy({"risk_level": 2})
    r = hub.submit_text("buy gel pens")
    assert r["status"] == "handed_off" and r["expected_total"] == 14.49
    assert r["cart_url"] == "/demo/cart/PEN-510?qty=1"
    assert "payment_token_minted" not in kinds(hub) and "handoff_created" in kinds(hub)
    assert hub.vault.tokens == {}


def test_approval_then_handoff(hub):
    r = hub.submit_text("buy a chair from staples")
    assert r["status"] == "awaiting_approval"
    o = hub.decide(r["approval_id"], True)
    assert o["status"] == "handed_off"
    row = hub.snapshot()["orders"][0]
    assert row["approved_by"] == "user" and row["requested_merchant"] == "staples"


def test_open_handoff_reserves_budget_and_skip_releases(hub):
    hub.set_policy({"risk_level": 2})
    r = hub.submit_text("buy gel pens")
    assert hub.spend() == (14.49, 14.49)
    assert hub.skip_handoff(r["order_id"])["status"] == "skipped"
    assert hub.spend() == (0, 0)


def test_mark_paid_as_proposed_and_changed(hub):
    hub.set_policy({"risk_level": 2})
    a = hub.submit_text("buy gel pens")
    b = hub.submit_text("buy coffee beans")
    pa = hub.mark_paid(a["order_id"], 14.49)
    assert pa["outcome"] == "as_proposed" and pa["merchant_order_id"].startswith("DEMO-")
    assert hub.mark_paid(b["order_id"], 27.10)["outcome"] == "changed"
    assert hub.spend()[0] == round(14.49 + 27.10, 2)  # actual charge replaces the estimate
    assert hub.mark_paid(a["order_id"], 14.49)["status"] == "error"  # only once
    t = hub.track_record()["overall"]
    assert (t["proposed"], t["as_proposed"], t["changed"], t["open"]) == (2, 1, 1, 0)


def test_trust_suggestion_then_pay_at_merchant(hub):
    hub.set_policy({"risk_level": 2})
    for _ in range(TRUST_MIN_PROPOSALS):
        r = hub.submit_text("buy gel pens")
        hub.mark_paid(r["order_id"], 14.49)
    sug = hub.track_record()["suggestions"]
    assert [s["merchant"] for s in sug] == ["demo-store"]
    hub.set_policy({"pay_merchants": ["demo-store"]})
    assert hub.track_record()["suggestions"] == []
    assert hub.submit_text("buy gel pens")["status"] == "ordered"


def test_no_suggestion_after_a_skip(hub):
    hub.set_policy({"risk_level": 2})
    for i in range(TRUST_MIN_PROPOSALS):
        r = hub.submit_text("buy gel pens")
        hub.skip_handoff(r["order_id"]) if i == 0 else hub.mark_paid(r["order_id"], 14.49)
    assert hub.track_record()["suggestions"] == []


def test_suggestion_uses_recent_proposals(hub):
    hub.set_policy({"risk_level": 2})
    r = hub.submit_text("buy gel pens")
    hub.skip_handoff(r["order_id"])
    for _ in range(TRUST_MIN_PROPOSALS):
        r = hub.submit_text("buy gel pens")
        hub.mark_paid(r["order_id"], 14.49)
    assert [s["merchant"] for s in hub.track_record()["suggestions"]] == ["demo-store"]


def test_invalid_payment_mode_ignored(hub):
    hub.set_policy({"payment_mode": "yolo"})
    assert hub.policy.payment_mode == "handoff"
