"""Any store on the web, by product URL.

Reads the price from schema.org JSON-LD or common meta tags. It never buys:
in handoff mode the product page *is* the cart and you pay there yourself.
The merchant is the site's domain (www.staples.com -> staples.com), so your
approved/blocked merchant lists and the per-store track record work per site.

Many large retailers block automated requests or only render prices with
JavaScript, so reading can fail. The hub then falls back to the price the
agent reported, marked unverified, and always asks you first.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.parse
import urllib.request

from . import MerchantAdapter, Product
from ..categories import classify

UA = "Mozilla/5.0 (Warden price monitor; personal use)"


def domain_of(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def check_public_url(url: str) -> None:
    """The hub fetches URLs an agent hands it, so refuse anything that isn't a
    public http(s) site: no file://, localhost, or private network addresses."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http(s) product URLs are allowed")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        # No local DNS (e.g. behind an HTTPS proxy, which resolves for us). The fetch
        # goes through the proxy or fails, and the request falls back to unverified.
        return
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"{parts.hostname} is not a public address")


def fetch_html(url: str) -> str | None:
    try:
        check_public_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        return urllib.request.urlopen(req, timeout=15).read(2_000_000).decode("utf-8", "ignore")
    except Exception:
        return None


def _find_offer(obj):
    if isinstance(obj, dict):
        if obj.get("@type") in ("Product", ["Product"]) or "offers" in obj:
            offers = obj.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("lowPrice")
                if price is not None:
                    cat = obj.get("category", "")
                    if isinstance(cat, dict):
                        cat = cat.get("name", "")
                    return {"name": obj.get("name", ""), "price": float(str(price).replace(",", "")),
                            "category": cat if isinstance(cat, str) else ""}
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


def parse_product(html: str) -> dict | None:
    """{"name", "price", "category"} from a product page, or None."""
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
        return {"name": t.group(1).strip() if t else "", "price": float(m.group(1).replace(",", "")), "category": ""}
    return None


def parse_price(html: str) -> tuple[str, float] | None:
    r = parse_product(html)
    return (r["name"], r["price"]) if r else None


class WebPriceReader(MerchantAdapter):
    name = "web"
    can_purchase = False  # Warden never pays here; purchases are handed off to you

    def search(self, query: str) -> list[Product]:
        return []

    def get_product(self, sku: str) -> Product | None:
        """`sku` is the product URL."""
        html = fetch_html(sku)
        r = parse_product(html) if html else None
        if not r or r["price"] <= 0:
            return None
        name = r["name"] or sku
        return Product(domain_of(sku), sku, name, r["price"], r["price"],
                       classify(name, r["category"], sku), sku)

    def cart_url(self, sku: str, quantity: int) -> str:
        return sku
