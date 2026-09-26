"""GOG: catalog import (names, ORIGINAL release dates, price, genres, DRM-free) and
installer sizes.

Verified against the live catalog response (2026-09): products have `releaseDate`
(original, e.g. 1998.07.31) AND `storeReleaseDate` (when GOG listed it) - we use the
former. `reviewsRating` is 0-50 (stars x10), so x2 = a 0-100 score.
Not verified: whether `searchAfter` takes the last product id (gogdb's own URL uses
searchAfter=0 with order=asc:externalProductId, which implies it); the loop stops
safely if a page adds nothing new. Installer `total_size` comes from the documented
products API (community docs); it is rate limited (~200 req/hour/IP) so we batch 50 ids.
"""
from __future__ import annotations

import math
import re
from typing import Iterator

from ..genres import norm_genres
from .base import Ingestor, Record, Refresher, Update

CATALOG = "https://catalog.gog.com/v1/catalog"
PRODUCTS = "https://api.gog.com/products"
PAGE = 48
_TM = re.compile(r"[™®©]")


def _date(s):
    m = re.fullmatch(r"(\d{4})\.(\d{2})\.(\d{2})", s or "")
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def record_from_product(p: dict) -> Record | None:
    title = _TM.sub("", p.get("title") or "").strip()
    if not p.get("id") or not title:
        return None
    rec = Record(source="gog", source_key=str(p["id"]), name=title, platform="pc")
    claims = [("drm", "drm-free", "store_label")]
    if p.get("productType"):
        claims.append(("category", p["productType"], None))
    money = (p.get("price") or {}).get("finalMoney") or {}
    if money.get("currency") == "USD" and money.get("amount") not in (None, ""):
        try:
            claims.append(("price", float(money["amount"]), None))
        except ValueError:
            pass
    d = _date(p.get("releaseDate"))
    if d:
        rec.release_date = d
        claims.append(("orig_year", int(d[:4]), None))
    genres = norm_genres(g.get("name") for g in p.get("genres") or [])
    if genres:
        claims.append(("genres", genres, None))
    if (p.get("reviewsCount") or 0) >= 5 and p.get("reviewsRating"):
        claims.append(("score", float(p["reviewsRating"]) * 2, None))
    rec.claims = claims
    return rec


class GogIngestor(Ingestor):
    name = "gog"

    def __init__(self, ctx, include_packs: bool = False):
        self.ctx, self.include_packs = ctx, include_packs

    def records(self) -> Iterator[Record]:
        types = "game,pack" if self.include_packs else "game"
        after, seen, n = "0", set(), 0
        while True:
            data = self.ctx.http.get_json(CATALOG, params={
                "limit": PAGE, "order": "asc:externalProductId", "productType": f"in:{types}",
                "countryCode": "US", "locale": "en-US", "currencyCode": "USD", "searchAfter": after},
                ttl=3600)
            products = data.get("products") or []
            fresh = [p for p in products if str(p.get("id")) not in seen]
            if not fresh:
                break
            total = data.get("productCount") or 0
            for p in fresh:
                seen.add(str(p["id"]))
                rec = record_from_product(p)
                if rec:
                    n += 1
                    yield rec
            self.ctx.progress(f"GOG catalog: {n} games", min(0.95, n / total) if total else None)
            if len(products) < PAGE:
                break
            after = str(products[-1]["id"])


def installer_size(product: dict) -> int | None:
    """Offline-installer total for the Windows/English build (largest if several)."""
    installers = (product.get("downloads") or {}).get("installers") or []
    if not installers:
        return None
    win = [i for i in installers if str(i.get("os", "")).lower() == "windows"] or installers
    en = [i for i in win if str(i.get("language", "")).lower() in ("en", "english")] or win
    sizes = [int(i["total_size"]) for i in en if isinstance(i.get("total_size"), (int, float)) and i["total_size"] > 0]
    return max(sizes) if sizes else None


class GogSizeRefresher(Refresher):
    name = "gog"
    fields = frozenset({"size"})

    def supports(self, row):
        return row["platform"] == "pc"

    def estimate(self, rows, fields, links):
        n = sum(1 for r in rows if r["id"] in links)
        return math.ceil(n / 50) * 18.5

    def fetch(self, rows, fields, links):
        todo = [(r["id"], links[r["id"]]) for r in rows if r["id"] in links]
        for i in range(0, len(todo), 50):
            chunk = todo[i:i + 50]
            data = self.ctx.http.get_json(PRODUCTS, params={
                "ids": ",".join(k for _, k in chunk), "expand": "downloads"}, ttl=86400)
            items = data if isinstance(data, list) else [data]
            by_id = {str(p.get("id")): p for p in items if isinstance(p, dict)}
            for rid, key in chunk:
                size = installer_size(by_id.get(key, {}))
                if size:
                    yield Update(rid, "size", size, "exact")
            self.ctx.progress(f"GOG sizes: {min(i + 50, len(todo))}/{len(todo)}", min(1.0, (i + 50) / len(todo)))
