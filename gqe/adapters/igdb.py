"""IGDB: true release years/dates, genres and review scores. Needs a free Twitch developer
app (Client ID + Secret): https://dev.twitch.tv/console/apps

Verified from IGDB docs: OAuth client-credentials via id.twitch.tv, `Client-ID` +
`Authorization: Bearer` headers, POST + Apicalypse body, 4 req/s, 500 rows/request,
`first_release_date` is unix seconds, `category` is deprecated (we don't use it).
Platform ids below are checked against IGDB at run time (see _verify_platforms), so a
wrong id fails loudly instead of silently mislabelling games.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from ..genres import norm_genres
from ..http import HttpError
from ..ingest import norm_name
from .base import Ingestor, Record, Refresher, Update

PLATFORM_IDS = {"pc": 6, "ps1": 7, "ps2": 8, "ps3": 9, "xbox": 11, "x360": 12,
                "gc": 21, "wii": 5, "dc": 23, "saturn": 32, "psp": 38}
# "=x" means exact (lowercase) name, otherwise substring
EXPECT = {6: "windows", 7: "=playstation", 8: "playstation 2", 9: "playstation 3", 11: "=xbox",
          12: "xbox 360", 21: "gamecube", 5: "wii", 23: "dreamcast", 32: "saturn",
          38: "playstation portable"}
FIELDS = ("name,alternative_names.name,first_release_date,total_rating,total_rating_count,"
          "genres.name,release_dates.date,release_dates.platform")
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API = "https://api.igdb.com/v4/"
WEEK = 7 * 86400
# IGDB game-type ids (stable enum; `category` is the deprecated name of the same thing):
# 0 main game, 4 standalone expansion, 8 remake, 9 remaster, 10 expanded game, 11 port.
# Everything else (DLC 1, expansion 2, bundle 3, mod 5, episode 6, season 7, fork 12, pack 13,
# update 14) is skipped by the catalog import so your library isn't flooded with add-ons.
IMPORT_TYPES = (0, 4, 8, 9, 10, 11)


def _iso(ts) -> str | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class IgdbApi:
    """Shared by the refresher and the catalog importer: auth, queries, platform checks."""
    needs = ("igdb.client_id", "igdb.client_secret")

    def __init__(self, ctx):
        self.ctx = ctx

    def _pids(self) -> dict:
        override = (self.ctx.config.get("igdb") or {}).get("platform_ids") or {}
        return {**PLATFORM_IDS, **override}

    # -- auth / transport
    def _token(self, force=False) -> str:
        st = self.ctx.state_get("igdb", "token")
        if st and not force and st["expires_at"] - time.time() > 86400:
            return st["access_token"]
        cfg = self.ctx.config["igdb"]
        data = self.ctx.http.post_json(TOKEN_URL, params={
            "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
            "grant_type": "client_credentials"})
        st = {"access_token": data["access_token"], "expires_at": time.time() + int(data.get("expires_in", 0))}
        self.ctx.state_set("igdb", "token", st)
        return st["access_token"]

    def _query(self, endpoint: str, body: str, ttl: float = 0):
        def go():
            return self.ctx.http.post_json(API + endpoint, body=body, ttl=ttl, headers={
                "Client-ID": self.ctx.config["igdb"]["client_id"],
                "Authorization": f"Bearer {self._token()}", "Accept": "application/json"})
        try:
            return go()
        except HttpError as e:
            if e.status == 401:          # token revoked/expired early: refresh once
                self._token(force=True)
                return go()
            raise

    def _verify_platforms(self, pids: set[int]) -> None:
        rows = self._query("platforms", f"fields name; where id = ({','.join(map(str, sorted(pids)))}); limit 50;", ttl=WEEK)
        names = {r["id"]: str(r.get("name", "")).lower() for r in rows}
        for pid in pids:
            want, got = EXPECT.get(pid), names.get(pid, "")
            if not want:
                continue
            ok = got == want[1:] if want.startswith("=") else want in got
            if not ok:
                raise LookupError(f"IGDB platform id {pid} is '{got or 'missing'}', expected '{want.lstrip('=')}'. "
                                  f"Set igdb.platform_ids in config.json to correct it.")



class IgdbRefresher(IgdbApi, Refresher):
    name = "igdb"
    fields = frozenset({"orig_year", "release_date", "genres", "score"})

    def supports(self, row):
        return row["platform"] in self._pids()

    # -- data
    def _bulk(self, pid: int) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self._query("games", f"fields {FIELDS}; where platforms = ({pid}) & version_parent = null; "
                                        f"sort id asc; limit 500; offset {offset};", ttl=WEEK)
            out += page
            self.ctx.progress(f"IGDB: downloaded {len(out)} games for platform {pid}", None)
            if len(page) < 500:
                return out
            offset += 500

    def _search(self, name: str, pid: int) -> list[dict]:
        safe = name.replace('"', " ")
        return self._query("games", f'search "{safe}"; fields {FIELDS}; '
                                    f"where platforms = ({pid}) & version_parent = null; limit 10;", ttl=WEEK)

    def _index(self, games: list[dict]) -> dict:
        idx: dict[str, list[dict]] = {}
        for g in games:
            names = [g.get("name", "")] + [a.get("name", "") for a in g.get("alternative_names") or []]
            for n in filter(None, names):
                idx.setdefault(norm_name(n), []).append(g)
        return idx

    @staticmethod
    def _best(cands: list[dict]) -> dict:
        return max(cands, key=lambda g: g.get("total_rating_count") or 0)

    def estimate(self, rows, fields, links):
        by: dict[str, int] = {}
        for r in rows:
            by[r["platform"]] = by.get(r["platform"], 0) + 1
        secs = 1.0
        for plat, n in by.items():
            secs += n * 0.3 if (plat == "pc" and n <= 150) else (300 if plat == "pc" else 12) * 0.3
        return secs

    def fetch(self, rows, fields, links):
        pids = self._pids()
        plats = sorted({r["platform"] for r in rows})
        self._verify_platforms({pids[p] for p in plats})
        for plat in plats:
            pid, todo = pids[plat], [r for r in rows if r["platform"] == plat]
            if plat == "pc" and len(todo) <= 150:
                lookup = lambda r, pid=pid: self._search(r["name"], pid)      # noqa: E731
                idx = None
            else:
                idx = self._index(self._bulk(pid))
                lookup = lambda r, idx=idx: idx.get(norm_name(r["name"]), [])  # noqa: E731
            matched = 0
            for r in todo:
                cands = lookup(r)
                if idx is None:
                    cands = [g for g in cands if norm_name(g.get("name", "")) == norm_name(r["name"])
                             or any(norm_name(a.get("name", "")) == norm_name(r["name"])
                                    for a in g.get("alternative_names") or [])]
                if not cands:
                    continue
                matched += 1
                yield from self._updates(r, self._best(cands), pid, fields)
            self.ctx.progress(f"IGDB {plat}: matched {matched}/{len(todo)} games", None)

    def _updates(self, row, g, pid, fields):
        rid = row["id"]
        if "orig_year" in fields and _iso(g.get("first_release_date")):
            yield Update(rid, "orig_year", int(_iso(g["first_release_date"])[:4]))
        if "release_date" in fields:
            plat_dates = [d["date"] for d in g.get("release_dates") or []
                          if d.get("platform") == pid and isinstance(d.get("date"), (int, float))]
            when = _iso(min(plat_dates)) if plat_dates else _iso(g.get("first_release_date"))
            if when:
                yield Update(rid, "release_date", when)
        if "genres" in fields:
            gen = norm_genres(x.get("name") for x in g.get("genres") or [])
            if gen:
                yield Update(rid, "genres", gen)
        if "score" in fields and g.get("total_rating") is not None:
            yield Update(rid, "score", round(float(g["total_rating"]), 1))


def _platform_date(g: dict, pid: int) -> str | None:
    dates = [d["date"] for d in g.get("release_dates") or []
             if d.get("platform") == pid and isinstance(d.get("date"), (int, float))]
    return _iso(min(dates)) if dates else _iso(g.get("first_release_date"))


class IgdbIngestor(IgdbApi, Ingestor):
    """Import EVERY game IGDB lists for the chosen platforms (default: PC).

    Only real games are imported (main games, remakes, remasters, ports, expanded games,
    standalone expansions); DLC, mods, bundles, episodes and updates are skipped, as are
    unreleased/undated entries unless asked for. Games you already have (matched by title or
    alternative title on the same platform) are not duplicated; they just get IGDB's
    year/genres/score filled in. New games arrive WITHOUT a size (IGDB has none).
    """
    name = "igdb"
    match_existing = True

    def __init__(self, ctx, platforms=("pc",), include_unreleased: bool = False):
        super().__init__(ctx)
        self.platforms, self.include_unreleased = tuple(platforms), include_unreleased

    def _check_type_ids(self) -> None:
        if not self._query("games", "fields id; where game_type = 0; limit 1;", ttl=WEEK):
            raise LookupError("IGDB returned nothing for game_type = 0, so its type ids may have changed. "
                              "Nothing was imported (importing without the type filter would add DLC and mods).")

    def records(self):
        pids = self._pids()
        plats = [p for p in self.platforms if p in pids]
        if not plats:
            raise LookupError("No supported platform selected.")
        self._verify_platforms({pids[p] for p in plats})
        self._check_type_ids()
        today = int(time.time()) // 86400 * 86400            # stable within a day so pages cache
        types = ",".join(map(str, IMPORT_TYPES))
        for plat in plats:
            pid, offset, n = pids[plat], 0, 0
            where = f"platforms = ({pid}) & version_parent = null & game_type = ({types})"
            if not self.include_unreleased:
                where += f" & first_release_date != null & first_release_date < {today}"
            while True:
                page = self._query("games", f"fields {FIELDS}; where {where}; sort id asc; limit 500; offset {offset};", ttl=WEEK)
                for g in page:
                    if not g.get("name"):
                        continue
                    n += 1
                    yield self._record(g, plat, pid)
                self.ctx.progress(f"IGDB {plat}: read {n} games", None)
                if len(page) < 500:
                    break
                offset += 500

    @staticmethod
    def _record(g: dict, plat: str, pid: int) -> Record:
        claims = []
        if _iso(g.get("first_release_date")):
            claims.append(("orig_year", int(_iso(g["first_release_date"])[:4]), None))
        gen = norm_genres(x.get("name") for x in g.get("genres") or [])
        if gen:
            claims.append(("genres", gen, None))
        if g.get("total_rating") is not None:
            claims.append(("score", round(float(g["total_rating"]), 1), None))
        return Record(source="igdb", source_key=f"{plat}:{g['id']}", name=g["name"], platform=plat,
                      release_date=_platform_date(g, pid), claims=claims,
                      aliases=[a["name"] for a in g.get("alternative_names") or [] if a.get("name")])


_STEAM_APP = re.compile(r"store\.steampowered\.com/app/(\d+)")


class IgdbSteamLinks(IgdbApi):
    """IGDB game id -> Steam app id(s), from IGDB's `external_games` (each entry holds the game's Steam
    store URL). We filter on the URL, not on the source/category field: IGDB retired `category` for these
    lookups (queries still using it silently match nothing) and the replacement ids are unverified here."""

    def mapping(self) -> dict[int, set[int]]:
        out: dict[int, set[int]] = {}
        offset, seen = 0, 0
        while True:
            try:
                page = self._query("external_games",
                                   'fields game,uid,url; where url ~ *"store.steampowered.com/app/"*; '
                                   f"sort id asc; limit 500; offset {offset};", ttl=WEEK)
            except HttpError as e:
                if e.status in (400, 401, 403, 404):
                    raise LookupError(f"IGDB refused the Steam-links lookup (HTTP {e.status}). "
                                      "Use the optional Steam key instead.") from None
                raise
            for e in page:
                m = _STEAM_APP.search(e.get("url") or "")
                if e.get("game") and m:
                    out.setdefault(int(e["game"]), set()).add(int(m.group(1)))
            seen += len(page)
            self.ctx.progress(f"IGDB: read {seen} Steam links", None)
            if len(page) < 500:
                return out
            offset += 500
