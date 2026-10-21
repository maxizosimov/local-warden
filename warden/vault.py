"""Payment vault.

The agent never sees card numbers. When the policy engine approves a
purchase, the vault mints a single-use token locked to ONE merchant and a
maximum amount (plus a small tolerance for tax/shipping). The token expires
after one use or 15 minutes.

MockVault is for testing. For real money, swap in a provider that issues
merchant-locked single-use virtual cards (e.g. Lithic, Stripe Issuing,
Privacy.com) - the interface stays the same.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

TOLERANCE = 1.15  # allow 15% over item total for tax/shipping


@dataclass
class Token:
    token: str
    merchant: str
    max_amount: float
    expires: float
    used: bool = False


class MockVault:
    name = "mock"

    def __init__(self):
        self.tokens: dict[str, Token] = {}

    def mint(self, merchant: str, amount: float) -> Token:
        t = Token("tok_" + secrets.token_hex(8), merchant, round(amount * TOLERANCE, 2), time.time() + 900)
        self.tokens[t.token] = t
        return t

    def charge(self, token: str, merchant: str, amount: float) -> tuple[bool, str]:
        t = self.tokens.get(token)
        if not t:
            return False, "unknown token"
        if t.used:
            return False, "token already used"
        if time.time() > t.expires:
            return False, "token expired"
        if t.merchant != merchant:
            return False, f"token locked to {t.merchant}"
        if amount > t.max_amount:
            return False, f"amount {amount} over token limit {t.max_amount}"
        t.used = True
        return True, "ok"
