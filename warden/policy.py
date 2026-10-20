"""Policy engine + risk dial.

Every purchase the agent wants to make becomes a `PurchaseIntent` and is
evaluated here BEFORE any money moves. The result is one of:

    allow             -> execute immediately
    require_approval  -> park in the approval queue for the human
    deny              -> never execute, logged with reasons

Hard limits (blocked merchants/categories, per-transaction max, daily and
monthly caps) apply at every risk level. The risk dial only decides how much
of the space *inside* those limits the agent may use without asking.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

RISK_LEVELS = {
    0: ("Observe", "Research and monitor only. No purchases, ever."),
    1: ("Ask always", "Every purchase waits for your approval."),
    2: ("Guarded", "Small buys at approved merchants go through; everything else asks."),
    3: ("Trusted", "Small buys at any non-blocked merchant go through; bigger ones ask."),
    4: ("Autonomous", "Anything inside your hard limits goes through without asking."),
}


@dataclass
class Policy:
    risk_level: int = 1
    max_per_transaction: float = 300.0      # hard ceiling, never exceeded
    auto_approve_under: float = 50.0        # levels 2-3: auto only below this
    daily_cap: float = 200.0
    monthly_cap: float = 600.0
    allowed_merchants: list[str] = field(default_factory=lambda: ["demo-store"])
    blocked_merchants: list[str] = field(default_factory=list)
    blocked_categories: list[str] = field(
        default_factory=lambda: ["gift_cards", "crypto", "firearms", "subscriptions"]
    )
    require_approval_categories: list[str] = field(default_factory=list)
    frontier_research_allowed: bool = True  # frontier models may research, never execute
    frontier_redact_pii: bool = True
    # "handoff": Warden prepares the cart and you pay; nothing is charged by Warden.
    # "pay": Warden pays with a single-use token. pay_merchants turns on "pay" for
    # individual merchants once the agent has earned it there.
    payment_mode: str = "handoff"
    pay_merchants: list[str] = field(default_factory=list)

    def pays_at(self, merchant: str) -> bool:
        return self.payment_mode == "pay" or merchant_in(merchant, self.pay_merchants)

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        p = Path(path)
        if not p.exists():
            pol = cls()
            pol.save(p)
            return pol
        data = json.loads(p.read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    def to_dict(self) -> dict:
        d = asdict(self)
        name, desc = RISK_LEVELS[self.risk_level]
        d["risk_name"], d["risk_description"] = name, desc
        return d


@dataclass
class PurchaseIntent:
    merchant: str
    item: str
    price: float
    quantity: int = 1
    category: str = "general"
    source: str = "local"  # who produced this intent, set by the hub: local | agent | frontier | user
    price_verified: bool = True  # False when Warden couldn't read the price and used the agent's

    @property
    def total(self) -> float:
        return round(self.price * self.quantity, 2)


@dataclass
class Decision:
    outcome: str  # allow | require_approval | deny
    reasons: list[str]

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "reasons": self.reasons}


def merchant_in(merchant: str, names: list[str]) -> bool:
    """'staples.com' in the list also covers 'shop.staples.com'."""
    m = merchant.lower()
    return any(m == x.lower() or m.endswith("." + x.lower()) for x in names)


def evaluate(policy: Policy, intent: PurchaseIntent, spent_today: float, spent_month: float) -> Decision:
    deny, ask = [], []
    m, total = intent.merchant.lower(), intent.total

    # ---- hard limits: apply at every risk level -------------------------
    if intent.source == "frontier":
        deny.append("Frontier models can research but are never allowed to execute purchases.")
    if policy.risk_level == 0:
        deny.append("Risk level 0 (Observe): purchases are disabled.")
    if merchant_in(m, policy.blocked_merchants):
        deny.append(f"Merchant '{intent.merchant}' is blocked.")
    if intent.category in policy.blocked_categories:
        deny.append(f"Category '{intent.category}' is blocked.")
    if not math.isfinite(total) or total <= 0:  # NaN would slip past every comparison below
        deny.append("Total must be a positive number.")
    if total > policy.max_per_transaction:
        deny.append(f"${total:.2f} exceeds per-transaction max ${policy.max_per_transaction:.2f}.")
    if spent_today + total > policy.daily_cap:
        deny.append(f"Would exceed daily cap (${spent_today:.2f} spent of ${policy.daily_cap:.2f}).")
    if spent_month + total > policy.monthly_cap:
        deny.append(f"Would exceed monthly cap (${spent_month:.2f} spent of ${policy.monthly_cap:.2f}).")
    if deny:
        return Decision("deny", deny)

    # ---- risk dial: inside the hard limits, how much can run unattended --
    allowed = merchant_in(m, policy.allowed_merchants)
    small = total < policy.auto_approve_under
    lvl = policy.risk_level

    if intent.category in policy.require_approval_categories:
        ask.append(f"Category '{intent.category}' always needs approval.")
    # These two ask at every risk level, even Autonomous.
    if not intent.price_verified:
        ask.append(f"Price not verified: Warden couldn't read the page, ${intent.price:.2f} is what the agent reported.")
    if intent.category == "unknown":
        ask.append("Couldn't tell what kind of product this is.")
    if lvl == 1:
        ask.append("Risk level 1 (Ask always).")
    elif lvl == 2:
        if not allowed:
            ask.append(f"'{intent.merchant}' is not on your approved-merchant list.")
        if not small:
            ask.append(f"${total:.2f} is above auto-approve limit ${policy.auto_approve_under:.2f}.")
    elif lvl == 3:
        if not small:
            ask.append(f"${total:.2f} is above auto-approve limit ${policy.auto_approve_under:.2f}.")

    if ask:
        return Decision("require_approval", ask)
    return Decision("allow", [f"Within policy at risk level {lvl} ({RISK_LEVELS[lvl][0]})."])
