"""Products from any website: price reading, merchant = domain, unverified prices, categories."""
import json

import pytest

from warden.categories import classify
from warden.merchants import REGISTRY, web
from warden.policy import Policy, PurchaseIntent, evaluate


def page(name, price, category=""):
    ld = {"@type": "Product", "name": name, "category": category, "offers": {"price": str(price)}}
    return f'<html><script type="application/ld+json">{json.dumps(ld)}</script></html>'


@pytest.fixture
def site(monkeypatch):
    """A fake internet: url -> html (None = blocked / unreadable)."""
    pages = {}
    monkeypatch.setattr(web, "fetch_html", lambda url: pages.get(url))
    monkeypatch.setattr(web, "check_public_url", lambda url: None)
    return pages


CHAIR = "https://www.staples.com/product/union-scale-mesh-chair"
CHAIR_HTML = page("Union & Scale Mesh Task Chair", 129.99, "Office Chairs")


# ---- parsing and safety -----------------------------------------------------
def test_parse_and_domain():
    assert web.parse_product(CHAIR_HTML) == {"name": "Union & Scale Mesh Task Chair", "price": 129.99,
                                             "category": "Office Chairs"}
    meta = '<title>Grill</title><meta property="product:price:amount" content="1,299.00">'
    assert web.parse_product(meta)["price"] == 1299.0
    assert web.domain_of(CHAIR) == "staples.com"
    assert web.domain_of("https://Shop.Example.co.uk./x") == "shop.example.co.uk"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost:8787/api/state", "http://127.0.0.1/",
                                 "http://10.0.0.1/admin", "http://169.254.169.254/latest/meta-data", "ftp://x.com/"])
def test_private_urls_refused(url):
    with pytest.raises(ValueError):
        web.check_public_url(url)


def test_public_ip_allowed():
    web.check_public_url("http://93.184.216.34/product")  # IP literal: no DNS needed


def test_categories():
    assert classify("Amazon.com eGift Card") == "gift_cards"
    assert classify("Visa $100", "", "https://x.com/gift-cards/visa-100") == "gift_cards"
    assert classify("Hot Glue Gun") != "firearms"
    assert classify("9mm Ammo, 50 rounds") == "firearms"
    assert classify("Premium Membership", "") == "subscriptions"
    assert classify("Mesh Task Chair", "Office Chairs") == "furniture"
    assert classify("Nerf Blaster Elite") == "unknown"  # no local model in tests


def test_policy_unverified_and_unknown_always_ask():
    pol = Policy(risk_level=4)
    assert evaluate(pol, PurchaseIntent("x.com", "a", 20, category="furniture"), 0, 0).outcome == "allow"
    assert evaluate(pol, PurchaseIntent("x.com", "a", 20, category="unknown"), 0, 0).outcome == "require_approval"
    assert evaluate(pol, PurchaseIntent("x.com", "a", 20, category="furniture", price_verified=False),
                    0, 0).outcome == "require_approval"
    blocked = Policy(risk_level=4, blocked_merchants=["staples.com"])
    assert evaluate(blocked, PurchaseIntent("shop.staples.com", "a", 20), 0, 0).outcome == "deny"


# ---- hub flows ----------------------------------------------------------------
def test_verified_url_hands_off_as_its_own_merchant(hub, site):
    site[CHAIR] = CHAIR_HTML
    hub.set_policy({"risk_level": 4})
    r = hub.submit_intent({"url": CHAIR, "reason": "you asked for a mesh chair"}, "external-agent")
    assert r["status"] == "handed_off" and r["cart_url"] == CHAIR and r["expected_total"] == 129.99
    o = hub.snapshot()["orders"][0]
    assert o["merchant"] == "staples.com" and o["category"] == "furniture"
    paid = hub.mark_paid(r["order_id"], 141.69)  # tax added at checkout
    assert paid["status"] == "paid" and paid["outcome"] == "changed"


def test_unreadable_page_needs_price_then_asks_even_at_autonomous(hub, site):
    hub.set_policy({"risk_level": 4})
    url = "https://www.homedepot.com/p/weber-grill"
    assert hub.submit_intent({"url": url}, "external-agent")["status"] == "error"
    r = hub.submit_intent({"url": url, "price": 179, "name": "Weber Spirit Gas Grill"}, "external-agent")
    assert r["status"] == "awaiting_approval" and r["product"]["category"] == "outdoor"
    assert any("Price not verified" in x for x in r["decision"]["reasons"])
    assert hub.decide(r["approval_id"], True)["status"] == "handed_off"


def test_unverified_price_that_turns_out_higher_aborts(hub, site):
    url = "https://www.homedepot.com/p/weber-grill"
    r = hub.submit_intent({"url": url, "price": 149, "name": "Weber Spirit Gas Grill"}, "external-agent")
    site[url] = page("Weber Spirit Gas Grill", 249)  # the page becomes readable: the agent lowballed
    assert hub.decide(r["approval_id"], True)["status"] == "aborted"


def test_gift_card_url_denied(hub, site):
    site["https://shop.example/visa"] = page("Visa Prepaid Card $150", 150)
    hub.set_policy({"risk_level": 4})
    r = hub.submit_intent({"url": "https://shop.example/visa"}, "external-agent")
    assert r["status"] == "denied" and "gift_cards" in r["message"]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1:8787/api/state"])
def test_private_url_refused_by_hub(hub, url):
    r = hub.submit_intent({"url": url, "price": 1, "name": "x"}, "external-agent")
    assert r["status"] == "error" and not hub.snapshot()["approvals"]


def test_warden_never_pays_on_websites(hub, site):
    site[CHAIR] = CHAIR_HTML
    hub.set_policy({"risk_level": 4, "payment_mode": "pay", "pay_merchants": ["staples.com"],
                    "daily_cap": 2000, "monthly_cap": 5000})
    for _ in range(5):
        r = hub.submit_intent({"url": CHAIR}, "external-agent")
        assert r["status"] == "handed_off"
        hub.mark_paid(r["order_id"], 129.99)
    hub.set_policy({"payment_mode": "handoff", "pay_merchants": []})
    assert hub.track_record()["suggestions"] == []  # can't offer to pay where Warden can't


def test_web_monitor_buy_hands_off(hub, site):
    site[CHAIR] = page("Union & Scale Mesh Task Chair", 129.99, "Office Chairs")
    hub.set_policy({"risk_level": 4})
    hub.add_monitor("web", CHAIR, "chair", 110, None, action="buy")
    assert hub.tick_monitors() == []
    site[CHAIR] = page("Union & Scale Mesh Task Chair", 99.99, "Office Chairs")
    fired = hub.tick_monitors()
    assert fired[0]["status"] == "handed_off"
    assert hub.snapshot()["orders"][0]["merchant"] == "staples.com"


def test_nan_price_cannot_bypass_limits(hub, site):
    site["https://evil.example/x"] = page("Mesh Chair", "NaN")
    hub.set_policy({"risk_level": 4})
    assert hub.submit_intent({"url": "https://evil.example/x"}, "external-agent")["status"] == "denied"
    r = hub.submit_intent({"url": "https://evil.example/y", "price": "inf", "name": "Mesh Chair"}, "external-agent")
    assert r["status"] == "denied"
