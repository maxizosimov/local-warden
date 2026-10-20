"""Read-only price reader for real product pages.

Point a monitor at any product URL; this pulls the price from schema.org
JSON-LD or common meta tags. It never buys anything. Many large retailers
block automated requests or change markup, so treat this as best-effort
and respect each site's terms of service.
"""
from __future__ import annotations

import json
import re
import urllib.request

from . import MerchantAdapter, Product

UA = "Mozilla/5.0 (Warden price monitor; personal use)"


def _find_offer(obj):
    if isinstance(obj, dict):
        if obj.get("@type") in ("Product", ["Product"]) or "offers" in obj:
            offers = obj.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice")
                if price is not None:
                    return obj.get("name", ""), float(str(price).replace(",", ""))
        for v in obj.values():
            r = _find_offer(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_offer(v)
            if r:
                return r
    return None


def parse_price(html: str) -> tuple[str, float] | None:
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            r = _find_offer(json.loads(block.strip()))
            if r:
                return r
        except (json.JSONDecodeError, ValueError):
            continue
    m = re.search(r'<meta[^>]+(?:product:price:amount|og:price:amount|itemprop="price")[^>]+content="([\d.,]+)"', html, re.I)
    if m:
        t = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        return (t.group(1).strip() if t else ""), float(m.group(1).replace(",", ""))
    return None


class WebPriceReader(MerchantAdapter):
    name = "web"
    can_purchase = False

    def search(self, query: str) -> list[Product]:
        return []

    def get_product(self, sku: str) -> Product | None:
        """`sku` is the product URL."""
        if not sku.lower().startswith(("http://", "https://")):
            return None  # never file:// or other schemes
        try:
            req = urllib.request.Request(sku, headers={"User-Agent": UA})
            html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
        except Exception:
            return None
        r = parse_price(html)
        if not r:
            return None
        name, price = r
        return Product(self.name, sku, name or sku, price, price, "general", sku)
