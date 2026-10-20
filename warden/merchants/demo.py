"""Demo store: a fake office/home catalog so anyone can test the full loop
(search -> policy -> approval -> order -> tracking -> price monitors)
without a real card or a real merchant account."""
from __future__ import annotations

import random
import threading
import time
import uuid

from . import MerchantAdapter, OrderResult, Product

CATALOG = [
    ("CHR-100", "Ergonomic Mesh Office Chair", 189.99, "furniture"),
    ("CHR-200", "Basic Task Chair", 79.99, "furniture"),
    ("DSK-300", "Standing Desk Converter", 229.00, "furniture"),
    ("GRL-400", "3-Burner Propane Grill", 349.00, "outdoor"),
    ("GRL-410", "Portable Charcoal Grill", 59.99, "outdoor"),
    ("PPR-500", "Copy Paper, 10 Reams", 54.99, "office_supplies"),
    ("PEN-510", "Gel Pens, 24 Pack", 14.49, "office_supplies"),
    ("INK-520", "Printer Ink Combo Pack", 42.99, "office_supplies"),
    ("COF-600", "Coffee Beans 2lb", 24.00, "grocery"),
    ("GFT-700", "$100 Gift Card", 100.00, "gift_cards"),
]

# Order lifecycle used to simulate shipping over time (seconds per stage in demo).
STAGES = ["placed", "processing", "shipped", "out_for_delivery", "delivered"]
STAGE_SECONDS = 45


class DemoStore(MerchantAdapter):
    name = "demo-store"
    can_purchase = True

    def __init__(self):
        self._lock = threading.Lock()
        self.products = {
            sku: Product(self.name, sku, n, p, p, c, f"demo://{sku}") for sku, n, p, c in CATALOG
        }
        self.orders: dict[str, dict] = {}

    # --- catalog -----------------------------------------------------------
    def search(self, query: str) -> list[Product]:
        words = [w for w in query.lower().split() if len(w) > 2]
        scored = []
        for p in self.products.values():
            hay = f"{p.name} {p.category}".lower()
            score = sum(1 for w in words if w.rstrip("s") in hay)
            if score:
                scored.append((score, p))
        if not scored:
            return []
        best = max(sc for sc, _ in scored)
        scored = [t for t in scored if t[0] == best]  # only the strongest matches
        scored.sort(key=lambda t: t[1].price)
        return [p for _, p in scored]

    def get_product(self, sku: str) -> Product | None:
        return self.products.get(sku)

    # --- test hooks: simulate the market ----------------------------------
    def set_sale(self, sku: str, pct_off: float) -> Product:
        with self._lock:
            p = self.products[sku]
            p.price = round(p.list_price * (1 - pct_off / 100), 2)
            return p

    def jitter(self) -> None:
        """Random small price moves + occasional sales, so monitors have something to watch."""
        with self._lock:
            for p in self.products.values():
                r = random.random()
                if r < 0.05:
                    p.price = round(p.list_price * random.choice([0.7, 0.75, 0.8]), 2)
                elif r < 0.25:
                    p.price = p.list_price

    # --- orders ------------------------------------------------------------
    def place_order(self, sku: str, quantity: int, payment_token: str) -> OrderResult:
        p = self.products.get(sku)
        if not p or not p.in_stock:
            return OrderResult(ok=False, error="out of stock")
        if not payment_token:
            return OrderResult(ok=False, error="no payment token")
        oid = "DEMO-" + uuid.uuid4().hex[:8].upper()
        self.orders[oid] = {"created": time.time(), "sku": sku, "qty": quantity}
        return OrderResult(ok=True, merchant_order_id=oid, status="placed", eta_days=3)

    def order_status(self, merchant_order_id: str) -> str:
        o = self.orders.get(merchant_order_id)
        if not o:
            return "unknown"
        idx = min(int((time.time() - o["created"]) // STAGE_SECONDS), len(STAGES) - 1)
        return STAGES[idx]
