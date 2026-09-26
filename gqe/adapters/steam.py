"""Steam store: price (USD), metacritic score, genres, store release date, sysreq-based
size ESTIMATE, and a 'steam' DRM label. PC games only.

Verified: `appdetails` needs no key; multiple appids only work with filters=price_overview
(so prices are fetched 50 at a time), full details are one app per request; ~200 req/5 min
so requests are spaced 1.6 s. Undocumented by Valve and may change. The Steam release date
is the STORE date (often years after the original), so it ranks below IGDB/GOG.
"""
from __future__ import annotations

import html
import math
import re
from datetime import datetime

from ..genres import norm_genres
from ..http import HttpError
from ..ingest import norm_name
from .base import Refresher, Update

SEARCH = "https://store.steampowered.com/api/storesearch/"
DETAILS = "https://store.steampowered.com/api/appdetails"
DAY = 86400
_TAG = re.compile(r"<[^>]+>")
_SIZE_A = re.compile(r"(?:storage|hard\s*(?:disk|drive)(?:\s*space)?|disk\s*space)\s*:?\s*(\d+(?:[.,]\d+)?)\s*(gb|mb)", re.I)
_SIZE_B = re.compile(r"(\d+(?:[.,]\d+)?)\s*(gb|mb)\s*(?:of\s*)?(?:available\s*)?(?:hard\s*(?:disk|drive)\s*)?(?:space|storage)", re.I)


def parse_release_date(text: str | None) -> str | None:
    text = (text or "").strip()
    for fmt, cut in (("%d %b, %Y", 10), ("%b %d, %Y", 10), ("%b %Y", 7), ("%B %d, %Y", 10), ("%d %B, %Y", 10), ("%Y", 4)):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")[:cut]
        except ValueError:
            continue
    m = re.search(r"\b(19|20)\d{2}\b", text)         # "Q3 2003"
    return m.group(0) if m and not text.lower().startswith("coming") else None


def sysreq_size(reqs) -> int | None:
    """Storage from the MINIMUM requirements text, as bytes (decimal GB/MB)."""
    minimum = reqs.get("minimum") if isinstance(reqs, dict) else None
    if not minimum:
        return None
    text = html.unescape(_TAG.sub(" ", minimum))
    m = _SIZE_A.search(text) or _SIZE_B.search(text)
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    return int(num * (10**9 if m.group(2).lower() == "gb" else 10**6))


class SteamRefresher(Refresher):
    name = "steam"
    fields = frozenset({"price", "score", "genres", "release_date", "size", "drm"})
    manual_only = frozenset({"size"})      # slow, developer-typed text: only when chosen explicitly

    def supports(self, row):
        return row["platform"] == "pc"

    def estimate(self, rows, fields, links):
        unlinked = sum(1 for r in rows if r["id"] not in links) if not self.ctx.state_get("steam", "matched_at") else 0
        n = len(rows)
        calls = math.ceil(n / 50) if set(fields) <= {"price"} else n
        return (unlinked + calls) * 1.6

    def _search(self, name: str):
        data = self.ctx.http.get_json(SEARCH, params={"term": name, "l": "english", "cc": "US"}, ttl=7 * DAY)
        want = norm_name(name)
        for it in data.get("items") or []:
            if it.get("id") and norm_name(it.get("name", "")) == want:
                return str(it["id"])
        return None

    def fetch(self, rows, fields, links):
        appids: dict[int, str] = {}
        searched = not self.ctx.state_get("steam", "matched_at")   # after offline matching, searching again is pointless
        for r in rows:
            aid = links.get(r["id"])
            if not aid and searched:
                try:
                    aid = self._search(r["name"])
                except HttpError:
                    aid = None
                if aid:
                    yield Update(r["id"], "link", aid)
            if aid:
                appids[r["id"]] = aid
        self.ctx.progress(f"Steam: found {len(appids)}/{len(rows)} games", None)
        if set(fields) <= {"price"}:
            yield from self._prices(appids)
        else:
            yield from self._full(appids, fields)

    def _prices(self, appids):
        items = list(appids.items())
        for i in range(0, len(items), 50):
            chunk = items[i:i + 50]
            data = self.ctx.http.get_json(DETAILS, params={
                "appids": ",".join(a for _, a in chunk), "filters": "price_overview", "cc": "us"}, ttl=DAY)
            for rid, aid in chunk:
                yield from self._price_update(rid, (data.get(aid) or {}).get("data"))
            self.ctx.progress(f"Steam prices: {min(i + 50, len(items))}/{len(items)}", min(1.0, (i + 50) / len(items)))

    @staticmethod
    def _price_update(rid, data):
        po = data.get("price_overview") if isinstance(data, dict) else None
        if po and po.get("currency") == "USD" and isinstance(po.get("final"), (int, float)):
            yield Update(rid, "price", po["final"] / 100)

    def _full(self, appids, fields):
        want = set(fields)
        for n, (rid, aid) in enumerate(appids.items(), 1):
            try:
                res = self.ctx.http.get_json(DETAILS, params={"appids": aid, "cc": "us", "l": "english"}, ttl=DAY)
            except HttpError:
                continue
            entry = res.get(aid) or {}
            d = entry.get("data") if entry.get("success") else None
            if not isinstance(d, dict):
                continue
            if "drm" in want:
                yield Update(rid, "drm", "steam", "store_label")
            if "price" in want:
                if d.get("is_free"):
                    yield Update(rid, "price", 0.0)
                else:
                    yield from self._price_update(rid, d)
            if "score" in want and (d.get("metacritic") or {}).get("score"):
                yield Update(rid, "score", float(d["metacritic"]["score"]))
            if "genres" in want:
                gen = norm_genres(g.get("description") for g in d.get("genres") or [])
                if gen:
                    yield Update(rid, "genres", gen)
            if "release_date" in want:
                when = parse_release_date((d.get("release_date") or {}).get("date"))
                if when:
                    yield Update(rid, "release_date", when)
            if "size" in want:
                size = sysreq_size(d.get("pc_requirements"))
                if size:
                    yield Update(rid, "size", size, "sysreq_estimate")
            self.ctx.progress(f"Steam details: {n}/{len(appids)}", n / len(appids))
