"""Merchant adapters.

An adapter knows how to search a store, read a product's live price, and
place an order. The hub never lets a model talk to an adapter directly:
search/price are open to the agent, but `place_order` is only ever called
by the executor after the policy engine says yes.

Shipped adapters:
  demo-store  - fake catalog with sales you can trigger, for safe testing
  web         - read-only price reader for any product URL (JSON-LD / meta tags)

To add a real store, subclass MerchantAdapter and register it in REGISTRY.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Product:
    merchant: str
    sku: str
    name: str
    price: float
    list_price: float
    category: str = "general"
    url: str = ""
    in_stock: bool = True

    @property
    def discount_pct(self) -> float:
        if self.list_price <= 0:
            return 0.0
        return round(100 * (1 - self.price / self.list_price), 1)

    def to_dict(self) -> dict:
        return {**self.__dict__, "discount_pct": self.discount_pct}


@dataclass
class OrderResult:
    ok: bool
    merchant_order_id: str = ""
    status: str = ""
    eta_days: int = 0
    error: str = ""


class MerchantAdapter(ABC):
    name: str = "base"
    can_purchase: bool = False

    @abstractmethod
    def search(self, query: str) -> list[Product]: ...

    @abstractmethod
    def get_product(self, sku: str) -> Product | None: ...

    def place_order(self, sku: str, quantity: int, payment_token: str) -> OrderResult:
        return OrderResult(ok=False, error=f"{self.name} adapter is read-only")

    def order_status(self, merchant_order_id: str) -> str:
        return "unknown"


from .demo import DemoStore  # noqa: E402
from .web import WebPriceReader  # noqa: E402

REGISTRY: dict[str, MerchantAdapter] = {}


def register(adapter: MerchantAdapter) -> None:
    REGISTRY[adapter.name] = adapter


register(DemoStore())
register(WebPriceReader())
