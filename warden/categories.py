"""Product categories, so blocked categories hold for products from any website.

Rules run first and always win for the categories you block by default
(gift cards, crypto, firearms, subscriptions): a page can't talk its way past
them. Otherwise a local model can classify, and anything unclear is
"unknown", which the policy engine sends to you for approval.
"""
from __future__ import annotations

import re

from . import models

CATEGORIES = ["furniture", "outdoor", "office_supplies", "grocery", "electronics", "home",
              "clothing", "toys", "health", "books", "gift_cards", "crypto", "firearms",
              "subscriptions"]
UNKNOWN = "unknown"

# Checked in order; the first four are the risky ones and are checked first.
RULES = [
    ("gift_cards", r"gift[\s-]?cards?|e-?gift|gift certificate|prepaid (?:visa|mastercard|card)|store credit"),
    ("crypto", r"bitcoin|\bbtc\b|ethereum|crypto|\busdt\b|stablecoin"),
    ("firearms", r"firearms?|\brifles?\b|\bpistols?\b|handguns?|shotguns?|revolvers?|ammunition|\bammo\b"),
    ("subscriptions", r"subscription|membership|auto[\s-]?renew|per month|/\s?mo(?:nth)?\b|monthly plan|annual plan"),
    ("furniture", r"\bchairs?\b|\bdesks?\b|\bsofa|\btables?\b|bookcase|shelving|dresser|mattress"),
    ("outdoor", r"\bgrills?\b|\bbbq\b|patio|lawn|garden|\btents?\b|camping"),
    ("office_supplies", r"\bpens?\b|pencils?|copy paper|\breams?\b|printer ink|toner|stapler|notebooks?|envelopes?"),
    ("grocery", r"coffee|\btea\b|snacks?|grocery|cereal|\bbeans\b"),
    ("electronics", r"laptop|monitor|headphones?|\bcharger|\bcables?\b|keyboard|\bmouse\b|tablet|camera|\btv\b|speaker"),
]


def rule_category(text: str) -> str | None:
    t = text.lower()
    for cat, pat in RULES:
        if re.search(pat, t):
            return cat
    return None


def classify(name: str, hint: str = "", url: str = "") -> str:
    path = re.sub(r"[-_/.]+", " ", url.split("?")[0])
    text = " ".join([name, hint, path])
    by_rule = rule_category(text)  # risky categories are first in RULES, so they win
    if by_rule:
        return by_rule
    raw = models.local_chat(
        "Classify the product into exactly one of: " + ", ".join(CATEGORIES) +
        ", unknown. Reply with the category only.", f"{name}\n{hint}", timeout=15)
    if raw:
        cat = raw.strip().lower().split()[0].strip(".,'\"") if raw.strip() else ""
        if cat in CATEGORIES:
            return cat
    return UNKNOWN
