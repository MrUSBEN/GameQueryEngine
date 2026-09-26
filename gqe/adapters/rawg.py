"""RAWG (rawg.io): release dates, genres and scores for CONSOLE games. Free API key required:
https://rawg.io/apidocs  (free for personal use, 20,000 requests/month; RAWG must be credited with a link,
which this app does in the Data tab, the README and the 'Score source' column).

Platform ids are found at run time by name (never hard-coded). Bulk method: one paged download per platform
(40 games per request), matched to your games by normalised title; unmatched games are left blank.
Score = Metacritic when RAWG has it, else RAWG's user rating (0-5) x 20 when it has >= 10 ratings.
A request counter per month stops the job well before the free allowance runs out.
Not verified from the build environment: exact response fields (built from RAWG's public docs); the fake-server
tests use those shapes.
"""
from __future__ import annotations

import time

from ..genres import norm_genres
from ..http import HttpError
from ..ingest import norm_name
from .base import Refresher, Update
from .redump import parse_any_date

API = "https://api.rawg.io/api/"
WEEK = 7 * 86400
MONTHLY_LIMIT = 18_000                       # of 20,000: leave headroom
ALIASES = {"ps1": ["playstation", "playstation 1", "ps1"], "ps2": ["playstation 2"], "ps3": ["playstation 3"],
           "psp": ["psp", "playstation portable"], "xbox": ["xbox"], "x360": ["xbox 360"], "gc": ["gamecube"],
           "wii": ["wii"], "dc": ["dreamcast"], "saturn": ["sega saturn", "saturn"]}


class RawgRefresher(Refresher):
    name = "rawg"
    fields = frozenset({"release_date", "orig_year", "genres", "score"})
    needs = ("rawg.api_key",)

    def supports(self, row):
        return row["platform"] in ALIASES

    def estimate(self, rows, fields, links):
        return len({r["platform"] for r in rows}) * 60.0

    # -- requests
    def _usage_key(self) -> str:
        return "usage:" + time.strftime("%Y-%m")

    def _get(self, path: str, params: dict):
        used = self.ctx.state_get("rawg", self._usage_key()) or 0
        if used >= MONTHLY_LIMIT:
            raise LookupError(f"RAWG's free allowance for this month is almost used ({used} of 20,000 requests). Try next month.")
        self.ctx.state_set("rawg", self._usage_key(), used + 1)
        try:
            return self.ctx.http.get_json(API + path, params={**params, "key": self.ctx.config["rawg"]["api_key"]}, ttl=WEEK)
        except HttpError as e:
            if e.status in (401, 403):
                raise LookupError(f"RAWG rejected the API key (HTTP {e.status}). Check it in Data > Console gaps.") from None
            raise

    def _platform_id(self, plat: str):
        cached = self.ctx.state_get("rawg", f"platform:{plat}")
        if cached:
            return cached
        wanted, page = set(ALIASES[plat]), 1
        while page < 10:
            data = self._get("platforms", {"page_size": 40, "page": page})
            for p in data.get("results") or []:
                if str(p.get("name", "")).strip().lower() in wanted:
                    self.ctx.state_set("rawg", f"platform:{plat}", p["id"])
                    return p["id"]
            if not data.get("next"):
                break
            page += 1
        return None

    def _bulk(self, pid) -> list[dict]:
        out, page = [], 1
        while page <= 400:
            data = self._get("games", {"platforms": pid, "page_size": 40, "page": page})
            results = data.get("results") or []
            out += results
            self.ctx.progress(f"RAWG: downloaded {len(out)} games for platform {pid}", None)
            if not data.get("next") or not results:
                break
            page += 1
        return out

    # -- matching
    def fetch(self, rows, fields, links):
        for plat in sorted({r["platform"] for r in rows}):
            pid = self._platform_id(plat)
            if pid is None:
                self.ctx.progress(f"RAWG has no platform called {ALIASES[plat][0]!r}; skipped {plat}", None)
                continue
            idx: dict[str, list[dict]] = {}
            for g in self._bulk(pid):
                idx.setdefault(norm_name(g.get("name", "")), []).append(g)
            matched = 0
            for r in (x for x in rows if x["platform"] == plat):
                cands = idx.get(norm_name(r["name"]))
                if not cands:
                    continue
                matched += 1
                g = max(cands, key=lambda c: c.get("ratings_count") or 0)
                yield from self._updates(r["id"], g, pid, fields)
            self.ctx.progress(f"RAWG {plat}: matched {matched} of {sum(1 for x in rows if x['platform'] == plat)} games", None)

    @staticmethod
    def _updates(rid, g, pid, fields):
        released = parse_any_date(g.get("released"))
        if "release_date" in fields:
            here = next((parse_any_date(p.get("released_at")) for p in g.get("platforms") or []
                         if (p.get("platform") or {}).get("id") == pid), None)
            when = here or released
            if when:
                yield Update(rid, "release_date", when)
        if "orig_year" in fields and released:
            yield Update(rid, "orig_year", int(released[:4]))
        if "genres" in fields:
            gen = norm_genres(x.get("name") for x in g.get("genres") or [])
            if gen:
                yield Update(rid, "genres", gen)
        if "score" in fields:
            mc = g.get("metacritic")
            if isinstance(mc, (int, float)) and mc > 0:
                yield Update(rid, "score", float(mc))
            elif (g.get("ratings_count") or 0) >= 10 and (g.get("rating") or 0) > 0:
                yield Update(rid, "score", round(float(g["rating"]) * 20, 1))
