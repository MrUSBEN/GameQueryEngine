"""Actions the UI (and CLI) trigger. Kept free of HTTP so they are easy to test."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Callable

from . import adapters
from .cancel import Cancelled
from .claims import FIELD_MAP, now_iso, record_claim
from .http import HttpError
from .ingest import upsert_release
from .query import select

Progress = Callable[[str, float | None], None]
_log = logging.getLogger("gqe.service")


def _noop(msg: str, frac: float | None = None) -> None:
    pass


def _find_existing(conn: sqlite3.Connection, rec) -> list[int]:
    """Release ids already in the database for this title (or an alias) on this platform."""
    from .ingest import norm_name
    from .platforms import normalize_platform
    plat = normalize_platform(rec.platform)
    found: list[int] = []
    for nn in dict.fromkeys(norm_name(n) for n in [rec.name, *rec.aliases] if n):
        if not nn:
            continue
        found += [r[0] for r in conn.execute(
            """SELECT r.id FROM releases r JOIN games g ON g.id = r.game_id
               WHERE g.name_norm = ? AND r.platform = ?""", (nn, plat))]
    return list(dict.fromkeys(found))


def _apply_claims(conn, rid: int, rec) -> None:
    if rec.release_date:
        record_claim(conn, rid, "release_date", rec.source, rec.release_date)
    for fld, value, conf in rec.claims:
        try:
            record_claim(conn, rid, fld, rec.source, value, conf)
        except (ValueError, KeyError):
            pass


def build_from_records(conn: sqlite3.Connection, ingestor, progress: Progress = _noop) -> dict:
    """Stream records into the database. If the ingestor sets `match_existing`, titles you
    already have are not duplicated: they only receive the source's claims."""
    match = getattr(ingestor, "match_existing", False)
    n = added = matched = refreshed = 0
    for rec in ingestor.records():
        n += 1
        existing = _find_existing(conn, rec) if match else []
        if existing:
            matched += 1
            for rid in existing:
                conn.execute("INSERT OR IGNORE INTO source_link(source, source_key, game_id, release_id) "
                             "SELECT ?,?,game_id,id FROM releases WHERE id=?", (rec.source, rec.source_key, rid))
                _apply_claims(conn, rid, rec)
        else:
            had = conn.execute("SELECT 1 FROM source_link WHERE source=? AND source_key=?",
                               (rec.source, rec.source_key)).fetchone()
            rid = upsert_release(conn, source=rec.source, source_key=rec.source_key, name=rec.name,
                                 platform=rec.platform, region=rec.region, size_bytes=rec.size_bytes,
                                 size_conf=rec.size_conf, discs=rec.discs, serial=rec.serial,
                                 release_date=rec.release_date)
            refreshed, added = (refreshed + 1, added) if had else (refreshed, added + 1)
            _apply_claims(conn, rid, rec)
        if n % 250 == 0:
            conn.commit()
            progress(f"Imported {n} ({added} new, {matched} already in your database)", None)
    conn.commit()
    return {"source": ingestor.name, "records": n, "added": added, "matched": matched, "refreshed": refreshed}


def _links(conn, source: str, release_ids: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for i in range(0, len(release_ids), 500):
        chunk = release_ids[i:i + 500]
        q = f"SELECT release_id, source_key FROM source_link WHERE source=? AND release_id IN ({','.join('?' * len(chunk))})"
        out.update({r[0]: r[1] for r in conn.execute(q, [source, *chunk])})
    return out


def _save_link(conn, source: str, key: str, release_id: int) -> None:
    row = conn.execute("SELECT game_id FROM releases WHERE id=?", (release_id,)).fetchone()
    if row:
        conn.execute("INSERT OR IGNORE INTO source_link(source, source_key, game_id, release_id) VALUES(?,?,?,?)",
                     (source, str(key), row[0], release_id))


def _eff(cls, source) -> frozenset:
    """Fields a source takes part in: slow `manual_only` fields only when it is chosen explicitly."""
    return frozenset(cls.fields) if source else frozenset(cls.fields) - frozenset(cls.manual_only)


def _pick(fields, source):
    classes = [c for c in adapters.REFRESHERS.values() if set(fields) & _eff(c, source)]
    if source:
        classes = [c for c in classes if c.name == source]
    if not classes:
        raise LookupError(f"No data source is installed that can fill {', '.join(fields)}"
                          + (f" from '{source}'" if source else "")
                          + f". Planned: {', '.join(adapters.PLANNED)}.")
    return classes


VIEW_COLUMN = {"size": "size_bytes", "price": "price", "score": "score", "genres": "genres", "drm": "drm",
               "release_date": "release_date", "orig_year": "orig_year", "category": "category"}


def all_fields() -> list[str]:
    """Every column at least one installed source can fill."""
    return [f for f in FIELD_MAP if any(f in _eff(c, None) for c in adapters.REFRESHERS.values())]


def _missing_map(rows: list[dict], fields: list[str]) -> dict[int, set]:
    return {r["id"]: {f for f in fields if r.get(VIEW_COLUMN[f]) in (None, "")} for r in rows}


def refresh_fields(conn: sqlite3.Connection, ctx, fields: list[str], release_ids: list[int],
                   source: str | None = None, progress: Progress = _noop,
                   only_missing: bool = False) -> dict:
    """Ask every source that can supply `fields` about exactly these releases.
    only_missing=True keeps every value you already have (from any source, including your own
    edits) and only fills empty cells; games with nothing missing are not even looked up."""
    if fields == ["all"]:
        fields, only_missing = all_fields(), True
    bad = [f for f in fields if f not in FIELD_MAP]
    if bad:
        raise ValueError(f"unknown column(s): {', '.join(bad)}")
    rows = _rows_by_id(conn, release_ids)
    missing = _missing_map(rows, fields) if only_missing else None
    ctx.progress = progress
    updated = skipped = 0
    notes: list[str] = []
    ran = 0
    could_cover = False
    for cls in _pick(fields, source):
        r = cls(ctx)
        absent = r.missing_config()
        problem = f"{r.name} needs setup ({', '.join(absent)}): add it in Data > Online sources" if absent else r.unavailable()
        if problem:
            if source:
                raise LookupError(problem)
            notes.append(problem)
            continue
        want = [f for f in fields if f in _eff(cls, source)]
        could_cover = could_cover or any(r.supports(row) for row in rows)
        todo = [row for row in rows if r.supports(row) and (not only_missing or missing[row["id"]] & set(want))]
        if only_missing:
            want = [f for f in want if any(f in missing[row["id"]] for row in todo)]
        if not todo:
            continue
        ran += 1
        progress(f"Asking {r.name} about {len(todo)} games", 0.0)
        links = _links(conn, r.link_source or r.name, [row["id"] for row in todo])
        try:
            for u in r.fetch(todo, want, links):
                if u.field == "link":
                    _save_link(conn, r.link_source or r.name, str(u.value), u.release_id)
                    continue
                if only_missing and u.field not in missing.get(u.release_id, ()):
                    continue                                   # already has a value: keep it
                try:
                    record_claim(conn, u.release_id, u.field, r.name, u.value, u.confidence)
                    updated += 1
                    if only_missing:
                        missing[u.release_id].discard(u.field)
                except (ValueError, KeyError):
                    skipped += 1
                if (updated + skipped) % 200 == 0:
                    conn.commit()
        except Cancelled:
            raise
        except (HttpError, LookupError, RuntimeError) as e:
            if source:                                   # you picked this source: tell you plainly
                raise
            conn.commit()
            note = f"{r.name} stopped: {e}"              # automatic mode: keep going with the other sources
            notes.append(note)
            _log.warning(note)
    conn.commit()
    if not ran and only_missing and could_cover and not notes:
        return {"fields": fields, "updated": 0, "skipped": 0, "asked": 0,
                "notes": ["Nothing to fill in: every game here already has a value for these columns."]}
    if not ran:
        raise LookupError("; ".join(notes) or
                          "None of the installed sources covers these games' platforms "
                          "(Steam and GOG are PC-only; IGDB covers PC and major consoles).")
    return {"fields": fields, "updated": updated, "skipped": skipped, "asked": len(rows), "notes": notes}


def refresh_field(conn, field, release_ids, source=None, progress: Progress = _noop, ctx=None):
    from .adapters import Context
    from .http import Http
    ctx = ctx or Context(conn, Http(), {})
    return refresh_fields(conn, ctx, [field], release_ids, source, progress)


def estimate_refresh(conn, ctx, fields: list[str], release_ids: list[int], source: str | None = None,
                     only_missing: bool = False) -> dict:
    if fields == ["all"]:
        fields, only_missing = all_fields(), True
    rows = _rows_by_id(conn, release_ids)
    missing = _missing_map(rows, fields) if only_missing else None
    seconds, sources, needs = 0.0, [], []
    looked: set[int] = set()
    for cls in _pick(fields, source):
        r = cls(ctx)
        if r.missing_config() or r.unavailable():
            needs.append(f"{r.name}: " + (r.unavailable() or f"needs setup ({', '.join(r.missing_config())})"))
            continue
        want = [f for f in fields if f in _eff(cls, source)]
        todo = [row for row in rows if r.supports(row) and (not only_missing or missing[row["id"]] & set(want))]
        if todo:
            sources.append(r.name)
            looked |= {row["id"] for row in todo}
            seconds += r.estimate(todo, want, _links(conn, r.link_source or r.name, [row["id"] for row in todo]))
    if not sources and not needs:
        needs.append("No installed source covers these games' platforms (Steam/GOG are PC-only).")
    return {"seconds": seconds, "count": len(rows), "sources": sources, "needs": needs,
            "to_look_up": len(looked)}


def update_source(conn, ctx, source: str, opts: dict, progress: Progress = _noop) -> dict:
    """The one-click actions on the Data tab."""
    ctx.progress = progress
    if source == "gog_catalog":
        from .adapters.gog import GogIngestor
        return build_from_records(conn, GogIngestor(ctx, include_packs=bool(opts.get("packs"))), progress)
    if source == "gog_sizes":
        ids = [r[0] for r in conn.execute(
            """SELECT r.id FROM releases r JOIN source_link l ON l.release_id=r.id AND l.source='gog'
               WHERE r.platform='pc' AND (r.size_bytes IS NULL OR r.size_conf!='exact')""")]
        return refresh_fields(conn, ctx, ["size"], ids, "gog", progress)
    if source == "igdb_all":
        from .adapters.igdb import PLATFORM_IDS
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM releases WHERE platform IN ({','.join('?' * len(PLATFORM_IDS))})", list(PLATFORM_IDS))]
        return refresh_fields(conn, ctx, ["orig_year", "release_date", "genres", "score"], ids, "igdb", progress)
    if source == "steam_match":
        return steam_match(conn, ctx, progress)
    if source == "steam_probe":
        return steam_probe(conn, ctx, progress, int(opts.get("count") or 200))
    if source == "steam_sizes":
        base = """FROM releases r JOIN source_link l ON l.release_id=r.id AND l.source='steam'
                  WHERE r.platform='pc' AND (r.size_bytes IS NULL OR r.size_conf!='exact')"""
        done = """AND EXISTS (SELECT 1 FROM claims c WHERE c.target='releases' AND c.row_id=r.id
                             AND c.field='size' AND c.source='steam_pics')"""
        skipped = conn.execute(f"SELECT COUNT(*) {base} {done}").fetchone()[0]
        ids = [r[0] for r in conn.execute(f"SELECT r.id {base} " + done.replace("AND EXISTS", "AND NOT EXISTS"))]
        _log.info("Steam sizes: %d games to ask, %d skipped (already have a Steam size)", len(ids), skipped)
        if not ids:
            return {"fields": ["size"], "updated": 0, "asked": 0, "skipped_already_sized": skipped,
                    "notes": ["Nothing to do: every linked game already has a size."]}
        out = refresh_fields(conn, ctx, ["size"], ids, "steam_pics", progress)
        out["skipped_already_sized"] = skipped
        return out
    if source == "steam_catalog":
        return steam_catalog(conn, ctx, progress)
    if source == "steamspy_scores":
        return steamspy_scores(conn, ctx, progress)
    if source == "steam_prices":
        ids = [r[0] for r in conn.execute(
            """SELECT r.id FROM releases r JOIN source_link l ON l.release_id=r.id AND l.source='steam'
               WHERE r.platform='pc' AND r.price_usd IS NULL""")]
        if not ids:
            return {"fields": ["price"], "updated": 0, "asked": 0, "notes": ["Nothing to do: every Steam game already has a price."]}
        return refresh_fields(conn, ctx, ["price"], ids, "steam", progress)
    if source == "wikidata_consoles":
        from .adapters.wikidata import LABELS as WD_LABELS
        marks = ",".join("?" * len(WD_LABELS))
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM v_release WHERE platform IN ({marks}) AND (year IS NULL OR orig_year IS NULL)", list(WD_LABELS))]
        if not ids:
            return {"fields": [], "updated": 0, "asked": 0, "notes": ["Nothing to do: no console game is missing a date."]}
        return refresh_fields(conn, ctx, ["release_date", "orig_year"], ids, "wikidata", progress, only_missing=True)
    if source == "rawg_consoles":
        from .adapters.rawg import ALIASES
        marks = ",".join("?" * len(ALIASES))
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM v_release WHERE platform IN ({marks}) AND (year IS NULL OR score IS NULL OR genres IS NULL)", list(ALIASES))]
        if not ids:
            return {"fields": [], "updated": 0, "asked": 0, "notes": ["Nothing to do: no console game is missing a year, score or genre."]}
        return refresh_fields(conn, ctx, ["release_date", "orig_year", "genres", "score"], ids, "rawg", progress, only_missing=True)
    if source == "steam_install":
        return install_steam_addon(ctx, progress)
    if source == "igdb_import":
        from .adapters.igdb import IgdbIngestor, PLATFORM_IDS
        from . import config as cfgmod
        miss = [k for k in IgdbIngestor.needs if not cfgmod.get(ctx.config, k)]
        if miss:
            raise LookupError("IGDB needs a Client ID and Secret first (Data > Online sources).")
        plats = [p for p in (opts.get("platforms") or ["pc"]) if p in PLATFORM_IDS]
        cfgmod.update({"igdb": {"import_platforms": plats}})   # remembered so Update can reuse the same choice
        return build_from_records(conn, IgdbIngestor(ctx, plats, bool(opts.get("unreleased"))), progress)
    raise ValueError(f"unknown source action {source!r}")


def _rows_by_id(conn, ids: list[int]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = f"SELECT * FROM v_release WHERE id IN ({','.join('?' * len(chunk))})"
        out += [dict(r) for r in conn.execute(q, chunk)]
    return out


def save_set(conn: sqlite3.Connection, name: str, release_ids: list[int]) -> int:
    conn.execute("DELETE FROM saved_set WHERE name=?", (name,))
    conn.executemany("INSERT OR IGNORE INTO saved_set VALUES(?,?)", [(name, i) for i in release_ids])
    conn.commit()
    return len(release_ids)


def list_sets(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT name, COUNT(*) AS n FROM saved_set GROUP BY name ORDER BY name")]


def delete_set(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM saved_set WHERE name=?", (name,))
    conn.commit()


def set_user_flag(conn: sqlite3.Connection, game_id: int, flag: str, value: bool) -> None:
    if flag not in ("favorite", "played"):
        raise ValueError("flag must be favorite or played")
    conn.execute("INSERT OR IGNORE INTO user_state(game_id) VALUES(?)", (game_id,))
    conn.execute(f"UPDATE user_state SET {flag}=? WHERE game_id=?", (1 if value else 0, game_id))
    conn.commit()


def set_user_flags(conn: sqlite3.Connection, clauses, flag: str, value: bool) -> dict:
    """Mark every game that has a release matching `clauses` as favorite/played (or not).
    Flags belong to the GAME, so marking the PS2 version also marks the PC version."""
    from .filters import compile_clauses
    if flag not in ("favorite", "played"):
        raise ValueError("flag must be favorite or played")
    where, params = compile_clauses(clauses)
    games = conn.execute(f"SELECT COUNT(DISTINCT game_id) FROM v_release WHERE {where}", params).fetchone()[0]
    conn.execute(f"INSERT OR IGNORE INTO user_state(game_id) SELECT DISTINCT game_id FROM v_release WHERE {where}", params)
    conn.execute(f"UPDATE user_state SET {flag}=? WHERE game_id IN (SELECT DISTINCT game_id FROM v_release WHERE {where})",
                 [1 if value else 0, *params])
    conn.commit()
    return {"games": games}


def facets(conn: sqlite3.Connection) -> dict:
    """Everything the UI needs to draw its filter controls from real data."""
    def col(sql):
        return [dict(r) for r in conn.execute(sql)]
    genres: dict[str, int] = {}
    for (g,) in conn.execute("SELECT genres FROM games WHERE genres IS NOT NULL"):
        for p in g.split("|"):
            if p:
                genres[p] = genres.get(p, 0) + 1
    regions: dict[str, int] = {}
    for (r,) in conn.execute("SELECT region FROM releases WHERE region IS NOT NULL"):
        for p in r.split(","):
            p = p.strip()
            if p:
                regions[p] = regions.get(p, 0) + 1
    rng = dict(conn.execute("""SELECT MIN(year) AS year_min, MAX(year) AS year_max,
                               MAX(size_bytes) AS size_max, MAX(price) AS price_max
                               FROM v_release""").fetchone())
    return {
        "platforms": col("SELECT platform AS value, COUNT(*) AS n FROM v_release GROUP BY platform ORDER BY n DESC"),
        "regions": [{"value": k, "n": v} for k, v in sorted(regions.items(), key=lambda kv: -kv[1])],
        "drm": col("SELECT drm AS value, COUNT(*) AS n FROM v_release WHERE drm IS NOT NULL GROUP BY drm ORDER BY n DESC"),
        "genres": [{"value": k, "n": v} for k, v in sorted(genres.items(), key=lambda kv: -kv[1])],
        "ranges": rng,
        "sets": list_sets(conn),
        "total": conn.execute("SELECT COUNT(*) FROM v_release").fetchone()[0],
    }


# ------------------------------------------------------------------ Steam: match, probe, add-on


def _steam_store_list(ctx, key: str):
    """Official list of Steam games (IStoreService, needs a free Web API key), paged 50k at a time."""
    from .http import HttpError
    last = 0
    while True:
        try:
            data = ctx.http.get_json("https://api.steampowered.com/IStoreService/GetAppList/v1/",
                                     params={"key": key, "max_results": 50000, "last_appid": last}, ttl=86400)
        except HttpError as e:
            if e.status in (401, 403):
                raise LookupError(f"Steam rejected the API key (HTTP {e.status}). Check it in Data > Steam sizes.") from None
            raise
        resp = data.get("response", data)
        apps = resp.get("apps") or []
        yield from apps
        if not apps or not resp.get("have_more_results"):
            return
        last = resp.get("last_appid") or apps[-1]["appid"]


def steam_match(conn: sqlite3.Connection, ctx, progress: Progress = _noop) -> dict:
    """Step 1: link your PC games to Steam app ids.
      A) exact, by ID: IGDB knows each game's Steam store page (uses the IGDB keys you already saved);
      B) by exact title from Steam's official game list, only if you saved a (free, optional) Steam key.
    Games with two candidate apps are skipped, never guessed. Afterwards Steam refreshes skip the slow
    per-game name search."""
    from . import config as cfgmod
    from .adapters.igdb import IgdbApi, IgdbSteamLinks
    from .ingest import norm_name
    have_igdb = all(cfgmod.get(ctx.config, k) for k in IgdbApi.needs)
    key = cfgmod.get(ctx.config, "steam.api_key")
    if not have_igdb and not key:
        raise LookupError("Nothing to match with yet: save your IGDB keys (Data > Online sources) or an optional "
                          "Steam key (Data > Steam sizes).")
    linked = {r[0] for r in conn.execute("SELECT release_id FROM source_link WHERE source='steam'")}
    rows = conn.execute("""SELECT r.id, g.name_norm FROM releases r JOIN games g ON g.id = r.game_id
                           WHERE r.platform = 'pc'""").fetchall()
    out = {"pc_games": len(rows), "already_linked": len(linked & {r[0] for r in rows}), "linked_via_igdb": 0,
           "linked_via_steam_list": 0, "ambiguous_skipped": 0, "unmatched": 0, "duplicate_skipped": 0,
           "methods": []}
    ambiguous: set[int] = set()

    def link(rid: int, appid: int, counter: str) -> None:
        cur = conn.execute("INSERT OR IGNORE INTO source_link(source, source_key, game_id, release_id) "
                           "SELECT 'steam', ?, game_id, id FROM releases WHERE id = ?", (str(appid), rid))
        if cur.rowcount:
            out[counter] += 1
            linked.add(rid)
        else:
            out["duplicate_skipped"] += 1

    if have_igdb:
        out["methods"].append("IGDB (exact IDs)")
        progress("Reading IGDB's Steam links", None)
        by_igdb: dict[int, int] = {}
        for rid, skey in conn.execute("""SELECT l.release_id, l.source_key FROM source_link l
                                         JOIN releases r ON r.id = l.release_id
                                         WHERE l.source='igdb' AND r.platform='pc'"""):
            if rid not in linked and ":" in skey and skey.split(":")[1].isdigit():
                by_igdb[rid] = int(skey.split(":")[1])
        _log.info("Steam match: %d PC games have an IGDB id and no Steam link yet", len(by_igdb))
        mapping = IgdbSteamLinks(ctx).mapping() if by_igdb else {}
        for rid, gid in by_igdb.items():
            apps = mapping.get(gid, set())
            if len(apps) == 1:
                link(rid, next(iter(apps)), "linked_via_igdb")
            elif len(apps) > 1:
                ambiguous.add(rid)
        conn.commit()
    if key:
        out["methods"].append("Steam game list (exact titles)")
        progress("Downloading Steam's official game list", None)
        index: dict[str, list[int]] = {}
        n = 0
        for a in _steam_store_list(ctx, key):
            n += 1
            nn = norm_name(a.get("name") or "")
            if nn:
                index.setdefault(nn, []).append(int(a["appid"]))
        _log.info("Steam match: %d games in Steam's list", n)
        for rid, nn in rows:
            if rid in linked:
                continue
            cands = index.get(nn, [])
            if len(cands) == 1:
                link(rid, cands[0], "linked_via_steam_list")
            elif len(cands) > 1:
                ambiguous.add(rid)
        conn.commit()
    out["ambiguous_skipped"] = len(ambiguous - linked)
    out["unmatched"] = sum(1 for rid, _ in rows if rid not in linked and rid not in ambiguous)
    out["drm_labelled"] = _backfill_steam_drm(conn)
    out["newly_linked"] = out["linked_via_igdb"] + out["linked_via_steam_list"]
    ctx.state_set("steam", "matched_at", now_iso())
    _log.info("Steam match done: %s", {k: v for k, v in out.items() if k != "methods"})
    return out


def _backfill_steam_drm(conn: sqlite3.Connection) -> int:
    """Every PC game sold on Steam gets the label 'steam' (needs the Steam client) unless a better source,
    e.g. GOG's 'drm-free', already outranks it. Finer detail (DRM-free Steam builds) needs PCGamingWiki."""
    ids = [r[0] for r in conn.execute(
        """SELECT l.release_id FROM source_link l JOIN releases r ON r.id = l.release_id
           WHERE l.source='steam' AND r.platform='pc' AND NOT EXISTS
             (SELECT 1 FROM claims c WHERE c.target='releases' AND c.row_id=r.id AND c.field='drm' AND c.source='steam')""")]
    for n, rid in enumerate(ids, 1):
        record_claim(conn, rid, "drm", "steam", "steam", "store_label")
        if n % 5000 == 0:
            conn.commit()
    conn.commit()
    return len(ids)


def _age_hours(iso) -> float:
    from datetime import datetime, timezone
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 3600
    except (TypeError, ValueError):
        return float("inf")


def _ts_date(ts):
    from datetime import datetime, timezone
    if not ts or ts < 315532800:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def steam_catalog(conn: sqlite3.Connection, ctx, progress: Progress = _noop) -> dict:
    """Add EVERY Steam game to the database. Two steps, each needing a different thing:
      1. The LIST of every app id - Steam's product-info system (PICS) has no cold "list everything" call;
         `get_changes_since` only updates a watchlist you already have (confirmed against the live service:
         a from-scratch request just says "do a full resync", with no list attached). So this step uses the
         official Web API (IStoreService/GetAppList), which needs a free Steam key (Data > Steam sizes).
      2. Reading each app's record (name, type, dates, genres, depot size) - PICS, anonymous, no key, via
         the same add-on used for sizes.
    Non-games (DLC, soundtracks, tools) and non-Windows apps are remembered and skipped on later runs. Titles
    you already have are linked, not duplicated; a same-titled but different game (release years disagree)
    stays a separate game. Resumable and cancelable."""
    from . import config as cfgmod
    from . import depots
    from .adapters.steam_pics import SteamPicsRefresher
    from .ingest import norm_name
    key = cfgmod.get(ctx.config, "steam.api_key")
    if not key:
        raise LookupError("Listing every Steam app needs a free Steam key (Data > Steam sizes has a 'How to "
                          "get one' guide) - Steam has no keyless way to list its whole catalog from scratch. "
                          "The add-on you already installed still measures sizes without one.")
    r = SteamPicsRefresher(ctx)
    if r.unavailable():
        raise LookupError(r.unavailable())
    cached = ctx.state_get("steam", "all_appids")
    if cached and _age_hours(cached.get("at")) < 20:
        ids = cached["ids"]
        _log.info("Steam catalog: using the app list fetched at %s (%d apps)", cached["at"], len(ids))
    else:
        progress("Asking Steam for the list of every app", None)
        ids = [int(a["appid"]) for a in _steam_store_list(ctx, key)]
        if not ids:
            raise LookupError("Steam's app list came back empty. Try again later.")
        ctx.state_set("steam", "all_appids", {"ids": ids, "at": now_iso()})
        _log.info("Steam catalog: Steam lists %d apps", len(ids))
    linked = {int(k) for (k,) in conn.execute("SELECT source_key FROM source_link WHERE source='steam'") if str(k).isdigit()}
    checked = {a for (a,) in conn.execute("SELECT appid FROM steam_checked")}
    todo = [a for a in ids if a not in linked and a not in checked]
    out = {"apps_on_steam": len(ids), "already_linked": sum(1 for a in ids if a in linked),
           "already_checked": sum(1 for a in ids if a in checked and a not in linked), "to_check": len(todo),
           "games_added": 0, "linked_to_existing": 0, "skipped_not_games": 0, "not_available": 0,
           "retry_later": 0, "different_game_same_title": 0}
    _log.info("Steam catalog: %d apps to check (%d already linked, %d already checked)", len(todo), out["already_linked"], out["already_checked"])
    for n, res in enumerate(r.run(todo, progress), 1):
        appid, summ = int(res["appid"]), res.get("summary")

        def mark(kind):
            conn.execute("INSERT OR REPLACE INTO steam_checked(appid, kind, checked_at) VALUES(?,?,?)", (appid, kind, now_iso()))
        status = res.get("status")
        if status == "error":
            # a request-level hiccup (e.g. the connection went stale mid-batch), not Steam confirming
            # there's no data: NOT recorded in steam_checked, so a future run tries these again.
            out["retry_later"] += 1
        elif status in ("unknown", "needs_token") or not summ:
            mark(status or "unknown")
            out["not_available"] += 1
        elif summ["type"] != "game" or not summ["windows"] or not summ.get("name"):
            mark(summ["type"] or "other")
            out["skipped_not_games"] += 1
        else:
            name, nn = summ["name"], norm_name(summ["name"])
            oy = int(_ts_date(summ["original_release"])[:4]) if _ts_date(summ["original_release"]) else None
            rid = None
            for cid, cyear in conn.execute(
                    """SELECT r.id, g.original_year FROM releases r JOIN games g ON g.id = r.game_id
                       WHERE g.name_norm = ? AND r.platform = 'pc' AND NOT EXISTS
                         (SELECT 1 FROM source_link l WHERE l.source='steam' AND l.release_id = r.id) ORDER BY r.id""", (nn,)).fetchall():
                if not (oy and cyear and abs(oy - cyear) > 2):
                    rid = cid
                    break
            if rid is not None:
                conn.execute("INSERT OR IGNORE INTO source_link(source, source_key, game_id, release_id) "
                             "SELECT 'steam', ?, game_id, id FROM releases WHERE id = ?", (str(appid), rid))
                out["linked_to_existing"] += 1
            else:
                gy = conn.execute("SELECT original_year FROM games WHERE name_norm=?", (nn,)).fetchone()
                apart = bool(gy and gy[0] and oy and abs(oy - gy[0]) > 2)
                rid = upsert_release(conn, source="steam", source_key=str(appid), name=name, platform="pc",
                                     name_norm=f"{nn}#steam{appid}" if apart else None)
                out["games_added"] += 1
                out["different_game_same_title"] += int(apart)
            when_o, when_s = _ts_date(summ["original_release"]), _ts_date(summ["steam_release"])
            if res.get("size"):
                record_claim(conn, rid, "size", "steam_pics", res["size"], "depot")
            if when_o or when_s:
                record_claim(conn, rid, "release_date", "steam", when_o or when_s)
            if when_o:
                record_claim(conn, rid, "orig_year", "steam", int(when_o[:4]))
            gen = depots.genres_from_ids(summ.get("genre_ids") or [])
            if gen:
                record_claim(conn, rid, "genres", "steam", gen)
            if summ.get("metacritic"):
                record_claim(conn, rid, "score", "steam", float(summ["metacritic"]))
            record_claim(conn, rid, "drm", "steam", "steam", "store_label")
            record_claim(conn, rid, "category", "steam", "game")
        if n % 200 == 0:
            conn.commit()
            progress(f"Steam catalog: {n}/{len(todo)} checked, {out['games_added']} added, {out['linked_to_existing']} linked", n / max(1, len(todo)))
    conn.commit()
    _log.info("Steam catalog done: %s", out)
    return out


_BAD_PAGE = object()   # sentinel: this page's text could not be read as JSON, even leniently


def _parse_steamspy_page(text: str):
    """Strict JSON first. Small PHP APIs sometimes prepend a stray notice/warning before the real JSON
    body (a very common real-world bug), so as a fallback, try parsing from the first '{' or '['."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if starts:
        try:
            return json.loads(text[min(starts):])
        except ValueError:
            pass
    return _BAD_PAGE


def steamspy_scores(conn: sqlite3.Connection, ctx, progress: Progress = _noop) -> dict:
    """Steam user-review scores (% positive) for games already linked to Steam, from SteamSpy's paged list
    (no key; unofficial; one page per minute, so a full run takes an hour or two). Only games with 10+ reviews get
    a score. Resumable: progress is saved after every page."""
    MIN_REVIEWS = 10
    MAX_CONSECUTIVE_BAD_PAGES = 3   # SteamSpy occasionally sends one malformed page; only give up if it's persistent
    st = ctx.state_get("steamspy", "progress") or {}
    page = st.get("page", 0) if st and _age_hours(st.get("at")) < 20 else 0
    links = {int(k): rid for rid, k in conn.execute(
        """SELECT l.release_id, l.source_key FROM source_link l JOIN releases r ON r.id = l.release_id
           WHERE l.source='steam' AND r.platform='pc'""") if str(k).isdigit()}
    out = {"resumed_from_page": page, "pages": 0, "apps_seen": 0, "scores_saved": 0, "genres_saved": 0,
           "too_few_reviews": 0, "bad_pages_skipped": 0}
    bad_in_a_row = 0
    while page < 500:
        try:
            text = ctx.http.request("GET", "https://steamspy.com/api.php", params={"request": "all", "page": page}, ttl=6 * 3600)
        except HttpError as e:
            raise LookupError(f"SteamSpy didn't answer at page {page} ({e}). Progress is saved: run it again later to resume.") from None
        if not text.strip():
            # SteamSpy signals "no more pages" with an empty body rather than an empty JSON object/array,
            # so this is the normal end of the list, not a broken response.
            _log.info("SteamSpy: page %d was empty; treating it as the end of the list", page)
            ctx.state_set("steamspy", "progress", {"page": 0, "at": now_iso(), "done": True})
            break
        data = _parse_steamspy_page(text)
        if data is _BAD_PAGE:
            bad_in_a_row += 1
            out["bad_pages_skipped"] += 1
            _log.warning("SteamSpy: page %d wasn't readable JSON (first 120 chars: %r); skipping it", page, text[:120])
            progress(f"SteamSpy: page {page} was unreadable, skipped ({out['scores_saved']} scores so far)", None)
            if bad_in_a_row >= MAX_CONSECUTIVE_BAD_PAGES:
                raise LookupError(f"SteamSpy sent unreadable data {bad_in_a_row} pages in a row (starting at page "
                                  f"{page - bad_in_a_row + 1}). Progress is saved up to the last good page: "
                                  "run it again later to resume.") from None
            page += 1
            ctx.state_set("steamspy", "progress", {"page": page, "at": now_iso()})
            continue
        bad_in_a_row = 0
        if not data:
            ctx.state_set("steamspy", "progress", {"page": 0, "at": now_iso(), "done": True})
            break
        for appid_s, g in (data.items() if isinstance(data, dict) else []):
            out["apps_seen"] += 1
            rid = links.get(int(appid_s)) if str(appid_s).isdigit() else None
            if rid is None:
                continue
            pos, neg = int(g.get("positive") or 0), int(g.get("negative") or 0)
            if pos + neg >= MIN_REVIEWS:
                record_claim(conn, rid, "score", "steamspy", round(100 * pos / (pos + neg), 1))
                out["scores_saved"] += 1
            else:
                out["too_few_reviews"] += 1
            if g.get("genre"):
                record_claim(conn, rid, "genres", "steamspy", g["genre"])
                out["genres_saved"] += 1
        conn.commit()
        page += 1
        out["pages"] += 1
        ctx.state_set("steamspy", "progress", {"page": page, "at": now_iso()})
        progress(f"SteamSpy: page {page} done ({out['scores_saved']} scores so far)", None)
    return out


CONSOLE_PLATFORMS = ["ps1", "ps2", "ps3", "psp", "gc", "wii", "xbox", "x360", "dc", "saturn"]

# Which platform(s) a Data-tab source run can affect, for the Status tab's "last checked" tracking.
# None here means "depends on options" (resolved by source_platforms()) or "spans everything".
SOURCE_PLATFORMS: dict[str, list[str] | None] = {
    "gog_catalog": ["pc"], "gog_sizes": ["pc"],
    "steam_catalog": ["pc"], "steam_match": ["pc"], "steam_probe": ["pc"],
    "steam_sizes": ["pc"], "steam_prices": ["pc"], "steamspy_scores": ["pc"], "steam_install": ["pc"],
    "igdb_all": None,            # every platform already in the database
    "igdb_import": None,         # resolved from opts["platforms"]
    "wikidata_consoles": CONSOLE_PLATFORMS,
    "rawg_consoles": CONSOLE_PLATFORMS,
    "redump": None,              # resolved from the .dat's detected/chosen platform
    "csv": None, "manual": None, "restore": None,
}


def source_platforms(source: str, opts: dict | None = None) -> list[str] | None:
    opts = opts or {}
    if source == "igdb_import":
        plats = opts.get("platforms")
        return list(plats) if plats else None
    if source == "redump":
        p = opts.get("platform")
        return [p] if p else None
    return SOURCE_PLATFORMS.get(source)


def log_activity(conn: sqlite3.Connection, kind: str, source: str, ok: bool,
                  summary: str | None = None, detail=None, platforms: list[str] | None = None) -> None:
    """Record that a source/import/refresh ran, whether or not it changed any data.
    This is what tells 'recently updated' (new claims) apart from 'recently checked' (ran, no change)."""
    conn.execute(
        "INSERT INTO activity_log(at, kind, source, platforms, ok, summary, detail) VALUES(?,?,?,?,?,?,?)",
        (now_iso(), kind, source, json.dumps(platforms) if platforms is not None else None,
         1 if ok else 0, (summary or "")[:500], json.dumps(detail, default=str) if detail is not None else None))
    conn.commit()


def _parse_iso(ts: str | None):
    if not ts:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _age_seconds(ts: str | None, now) -> float | None:
    dt = _parse_iso(ts)
    return (now - dt).total_seconds() if dt else None


# Thresholds for the Status tab's color coding.
FRESH_S = 2 * 24 * 3600      # "recently updated" (green)
CHECKED_S = 14 * 24 * 3600   # "recently checked, no change" (yellow); older = "stale" (orange)
BAD_MISSING_PCT = 40.0       # missing this much of a field = "problematic" (red), regardless of recency


def _status_color(missing_pct: float | None, last_updated: str | None, last_checked: str | None, now) -> str:
    if missing_pct is not None and missing_pct >= BAD_MISSING_PCT:
        return "red"
    ua = _age_seconds(last_updated, now)
    if ua is not None and ua <= FRESH_S:
        return "green"
    ca = _age_seconds(last_checked, now)
    if ca is not None and ca <= CHECKED_S:
        return "yellow"
    if ua is not None or ca is not None:
        return "orange"
    return "red" if (missing_pct or 0) > 0 else "orange"


def status_report(conn: sqlite3.Connection, db_path) -> dict:
    """Everything the Status tab shows: a database-wide summary, per-platform overview and a
    detailed, color-coded breakdown of each platform's fields, backed by claim timestamps
    (what actually changed) and the activity log (what was last run/checked)."""
    from datetime import datetime, timezone
    import os
    now = datetime.now(timezone.utc)

    counts = {k: conn.execute(f"SELECT COUNT(*) FROM {k}").fetchone()[0] for k in ("games", "releases", "claims")}
    try:
        st = os.stat(db_path)
        file_info = {"path": str(db_path), "size": st.st_size,
                     "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()}
    except OSError:
        file_info = {"path": str(db_path), "size": None, "modified": None}
    schema_version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    folder = (db_path.parent / "backups") if hasattr(db_path, "parent") else None
    safety_copies = []
    if folder and folder.exists():
        for p in sorted(folder.glob("*.db"))[-5:]:
            safety_copies.append({"name": p.name, "modified": datetime.fromtimestamp(
                p.stat().st_mtime, tz=timezone.utc).isoformat()})

    # Claims -> when each source last touched the database, and how much it has contributed.
    sources = {}
    for row in conn.execute("SELECT source, COUNT(*) AS n, MAX(fetched_at) AS last FROM claims GROUP BY source"):
        sources[row["source"]] = {"source": row["source"], "claims": row["n"], "last_claim": row["last"]}
    for row in conn.execute("SELECT source, MAX(at) AS last, SUM(ok) AS ok_n, COUNT(*) AS n FROM activity_log GROUP BY source"):
        s = sources.setdefault(row["source"], {"source": row["source"], "claims": 0, "last_claim": None})
        s["last_run"], s["runs"], s["runs_ok"] = row["last"], row["n"], row["ok_n"]
    last_ok_by_source: dict[str, bool] = {}
    for row in conn.execute("SELECT source, ok FROM activity_log ORDER BY id DESC"):
        last_ok_by_source.setdefault(row["source"], bool(row["ok"]))
    for s in sources.values():
        s.setdefault("last_run", None)
        last = max((t for t in (s.get("last_claim"), s.get("last_run")) if t), default=None)
        s["last_activity"] = last
        last_ok = last_ok_by_source.get(s["source"])
        s["color"] = "red" if last_ok is False else _status_color(None, s.get("last_claim"), last, now)

    # Per-platform coverage (same numbers the Data tab used to show).
    from . import query as _query
    platform_rows = _query.stats(conn)
    gaps = gaps_report(conn)
    gaps_by_platform = {g["platform"]: g for g in gaps}

    # When each platform's releases/games claims last changed, from the claims table itself.
    upd_rows = conn.execute("""
        SELECT platform, MAX(fetched_at) AS last, MIN(field) AS f FROM (
            SELECT r.platform AS platform, c.fetched_at AS fetched_at, c.field AS field
              FROM claims c JOIN releases r ON r.id = c.row_id WHERE c.target='releases'
            UNION ALL
            SELECT r.platform AS platform, c.fetched_at AS fetched_at, c.field AS field
              FROM claims c JOIN games g ON g.id = c.row_id JOIN releases r ON r.game_id = g.id
              WHERE c.target='games'
        ) GROUP BY platform""").fetchall()
    last_updated_by_platform = {r["platform"]: r["last"] for r in upd_rows}

    # Per (platform, field) last update, for the detailed field-level coloring.
    field_upd = {}
    for r in conn.execute("""
        SELECT r.platform AS platform, c.field AS field, MAX(c.fetched_at) AS last
          FROM claims c JOIN releases r ON r.id = c.row_id WHERE c.target='releases' GROUP BY r.platform, c.field"""):
        field_upd[(r["platform"], r["field"])] = r["last"]
    for r in conn.execute("""
        SELECT r.platform AS platform, c.field AS field, MAX(c.fetched_at) AS last
          FROM claims c JOIN games g ON g.id = c.row_id JOIN releases r ON r.game_id = g.id
          WHERE c.target='games' GROUP BY r.platform, c.field"""):
        prev = field_upd.get((r["platform"], r["field"]))
        field_upd[(r["platform"], r["field"])] = max(prev, r["last"]) if prev else r["last"]

    # Activity log -> last time each platform was "checked" by some source, whether or not it
    # changed anything, plus which sources cover which fields (for the field-level cells).
    activity_rows = conn.execute("SELECT at, source, platforms, ok FROM activity_log ORDER BY id DESC LIMIT 500").fetchall()
    last_checked_by_platform: dict[str, str] = {}
    last_checked_by_source: dict[str, str] = {}
    for r in activity_rows:
        plats = json.loads(r["platforms"]) if r["platforms"] else None
        last_checked_by_source[r["source"]] = max(last_checked_by_source.get(r["source"], ""), r["at"])
        targets = plats if plats is not None else [p["platform"] for p in platform_rows]
        for p in targets:
            last_checked_by_platform[p] = max(last_checked_by_platform.get(p, ""), r["at"])

    from .claims import PRECEDENCE
    FIELD_LABELS = {"year": "Release year", "score": "Review score", "genre": "Genre",
                    "size": "Size", "price": "Price", "drm": "DRM"}
    field_to_canonical = {"genre": "genres", "year": "release_date"}  # gaps_report labels -> claim field names

    platforms_out = []
    for p in platform_rows:
        plat = p["platform"]
        last_updated = last_updated_by_platform.get(plat)
        last_checked = last_checked_by_platform.get(plat)
        g = gaps_by_platform.get(plat, {"columns": {}, "total": p["releases"]})
        total = g["total"] or 1
        overall_missing = 100.0 * sum(c["missing"] for c in g["columns"].values()) / (total * max(len(g["columns"]), 1))
        color = _status_color(overall_missing, last_updated, last_checked, now)
        fields = {}
        for label, col in g["columns"].items():
            cfield = field_to_canonical.get(label, label)
            fu = field_upd.get((plat, cfield))
            src_names = PRECEDENCE.get(cfield, [])
            fc = max((last_checked_by_source.get(s, "") for s in src_names), default="") or None
            miss_pct = round(100.0 * col["missing"] / total, 1)
            fields[label] = {"label": FIELD_LABELS.get(label, label.title()), "missing": col["missing"],
                             "missing_pct": miss_pct, "examples": col["examples"],
                             "last_updated": fu, "last_checked": fc,
                             "color": _status_color(miss_pct, fu, fc, now)}
        platforms_out.append({
            "platform": plat, "releases": p["releases"], "stats": p, "color": color,
            "last_updated": last_updated, "last_checked": last_checked,
            "overall_missing_pct": round(overall_missing, 1), "fields": fields})

    recent = [{"at": r["at"], "kind": r["kind"], "source": r["source"],
               "platforms": json.loads(r["platforms"]) if r["platforms"] else None,
               "ok": bool(r["ok"]), "summary": r["summary"]}
              for r in conn.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 20")]
    last_library_update = conn.execute(
        "SELECT MAX(at) FROM activity_log WHERE source LIKE 'update_library:%'").fetchone()[0]

    return {
        "generated_at": now.isoformat(), "counts": counts, "file": file_info,
        "schema_version": int(schema_version[0]) if schema_version else None,
        "safety_copies": safety_copies, "last_library_update": last_library_update,
        "sources": sorted(sources.values(), key=lambda s: s["source"]),
        "platforms": platforms_out, "recent_activity": recent,
    }


def gaps_report(conn: sqlite3.Connection, per: int = 10) -> list[dict]:
    """Per platform: how many games lack year / score / genre / price / drm (and size for PC),
    with random example titles, so matching problems can be seen rather than guessed."""
    checks = [("year", "year IS NULL"), ("score", "score IS NULL"), ("genre", "genres IS NULL"),
              ("price", "price IS NULL"), ("drm", "drm IS NULL")]
    out = []
    for (plat, total) in conn.execute("SELECT platform, COUNT(*) FROM v_release GROUP BY platform ORDER BY COUNT(*) DESC").fetchall():
        entry = {"platform": plat, "total": total, "columns": {}}
        for label, cond in checks + ([("size", "size_bytes IS NULL")] if plat == "pc" else []):
            n = conn.execute(f"SELECT COUNT(*) FROM v_release WHERE platform=? AND {cond}", (plat,)).fetchone()[0]
            ex = [r[0] for r in conn.execute(f"SELECT name FROM v_release WHERE platform=? AND {cond} ORDER BY RANDOM() LIMIT ?", (plat, per))]
            entry["columns"][label] = {"missing": n, "examples": ex}
        out.append(entry)
    return out


def steam_probe(conn: sqlite3.Connection, ctx, progress: Progress = _noop, count: int = 200) -> dict:
    """Step 2: try the depot-size method on a random sample WITHOUT saving anything, and report how
    well it works (coverage, and agreement with GOG's exact installer sizes where both exist)."""
    from statistics import median
    from .adapters.steam_pics import SteamPicsRefresher
    r = SteamPicsRefresher(ctx)
    problem = r.unavailable()
    if problem:
        raise LookupError(problem)
    rows = conn.execute("""SELECT r.id, g.name, l.source_key FROM releases r
                           JOIN games g ON g.id = r.game_id
                           JOIN source_link l ON l.release_id = r.id AND l.source = 'steam'
                           WHERE r.platform = 'pc' ORDER BY RANDOM() LIMIT ?""", (count,)).fetchall()
    if not rows:
        raise LookupError("No games are linked to Steam yet. Run step 1 (Match my games to Steam) first.")
    by_app = {int(k): (rid, name) for rid, name, k in rows}
    total_linked = conn.execute("SELECT COUNT(*) FROM source_link l JOIN releases r ON r.id=l.release_id "
                                "WHERE l.source='steam' AND r.platform='pc'").fetchone()[0]
    t0 = time.monotonic()
    results = list(r.run(list(by_app), progress))
    elapsed = max(time.monotonic() - t0, 0.001)
    census: dict[str, int] = {}
    stats = {"asked": len(by_app), "with_size": 0, "manifest_sizes": 0, "maxsize_only": 0,
             "no_size": 0, "not_a_game": 0, "not_returned": 0, "has_original_date": 0}
    examples, ratios = [], []
    seen = set()
    for res in results:
        for k in (res.get("summary") or {}).get("keys", []):
            census[k] = census.get(k, 0) + 1
        rid, name = by_app.get(res["appid"], (None, None))
        seen.add(res["appid"])
        if res.get("size"):
            stats["with_size"] += 1
            stats["manifest_sizes" if res["kind"] == "manifest" else "maxsize_only"] += 1
            if res.get("original_year"):
                stats["has_original_date"] += 1
            if len(examples) < 8:
                examples.append({"name": name, "gb": round(res["size"] / 1e9, 2), "kind": res["kind"], "depots": res["depots"]})
            gog = conn.execute("SELECT value FROM claims WHERE target='releases' AND row_id=? AND field='size' AND source='gog'", (rid,)).fetchone()
            if gog:
                ratios.append({"name": name, "steam_gb": round(res["size"] / 1e9, 2), "gog_gb": round(int(gog[0]) / 1e9, 2),
                               "ratio": round(res["size"] / int(gog[0]), 2)})
        elif res.get("status") == "not_a_game":
            stats["not_a_game"] += 1
        else:
            stats["no_size"] += 1
    stats["not_returned"] = len(set(by_app) - seen)
    stats["median_gb"] = round(median(x["gb"] for x in examples), 2) if examples else None
    stats.update({"elapsed_s": round(elapsed, 1), "games_per_sec": round(len(by_app) / elapsed, 2),
                  "total_linked": total_linked,
                  "projected_minutes": round(total_linked / (len(by_app) / elapsed) / 60, 1)})
    _log.info("Steam probe: %s", stats)
    from .depots import CENSUS_KEYS
    return {"probe": True, **stats, "census": {k: census.get(k, 0) for k in CENSUS_KEYS},
            "census_total": sum(1 for r_ in results if r_.get("summary")), "examples": examples, "compared_with_gog": ratios[:10],
            "median_ratio_vs_gog": round(median(x["ratio"] for x in ratios), 2) if ratios else None,
            "compared_count": len(ratios)}


def install_steam_addon(ctx, progress: Progress = _noop) -> dict:
    """Install the optional `steam` client package (runs pip; needs internet)."""
    import subprocess
    import sys
    runner = ctx.services.get("pip") or (lambda: subprocess.run(
        [sys.executable, "-m", "pip", "install", "steam[client]"], capture_output=True, text=True, timeout=900))
    progress("Installing the Steam add-on (this can take a minute)", None)
    res = runner()
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError("Installing failed: " + " | ".join(tail))
    return {"installed": True}


# ------------------------------------------------------------------ Update: for a database you already have
MIN_GAMES_FOR_UPDATE = 50   # below this, "Update" almost certainly means "I haven't imported anything yet"


class DatabaseTooEmpty(LookupError):
    pass


def _step_line(name: str, result: dict | None) -> str:
    if result is None:
        return f"{name}: skipped"
    if "games_added" in result:                                              # steam_catalog
        return (f"{name}: {result['games_added']} new, {result['linked_to_existing']} linked to games "
                f"you had, {result['skipped_not_games']} not real games")
    if "added" in result and "matched" in result:                            # build_from_records (GOG/IGDB import)
        return f"{name}: {result.get('records', 0)} read, {result['added']} new, {result['matched']} already had"
    if "newly_linked" in result:                                             # steam_match
        return f"{name}: {result['newly_linked']} newly linked to Steam"
    if "scores_saved" in result:                                             # steamspy_scores
        return f"{name}: {result['scores_saved']} scores, {result['genres_saved']} genre tags"
    if "updated" in result and "asked" in result:                            # refresh_fields
        return f"{name}: {result['updated']} value(s) filled ({result['asked']} games checked)"
    return f"{name}: done"


def update_library(conn: sqlite3.Connection, ctx, mode: str, progress: Progress = _noop) -> dict:
    """The single button for a database you've already built: brings in games released since your last
    visit, and (in 'new_and_fill' mode) tries to fill anything still missing for games you already have.

    Composed from the same actions available individually elsewhere, run one after another. A source
    that isn't set up (no key, no add-on) is skipped with a note rather than stopping the rest - the
    point is to make progress with whatever IS configured, not to demand everything at once.
    Refuses to run on a database that looks essentially empty, since Update assumes there is already
    something worth updating; building the first copy is what the numbered sources further down are for.
    """
    from . import config as cfgmod
    from .adapters.igdb import IgdbApi, IgdbIngestor, PLATFORM_IDS
    from .adapters.gog import GogIngestor

    if mode not in ("new", "new_and_fill"):
        raise ValueError("mode must be 'new' or 'new_and_fill'")
    total = conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
    if total < MIN_GAMES_FOR_UPDATE:
        raise DatabaseTooEmpty(
            "Your database doesn't have much in it yet, so there's nothing to bring up to date. Update "
            "is for topping up a database you've already built - use the numbered sources below first "
            "(the GOG catalog import needs no key and is the fastest place to start), then come back "
            "here whenever you want to check for anything new.")

    lines: list[str] = []
    notes: list[str] = []
    results: dict[str, dict | None] = {}

    def step(name: str, fn, needs_ok: bool) -> None:
        if not needs_ok:
            notes.append(f"{name}: skipped (not set up yet)")
            lines.append(f"{name}: skipped (not set up yet)")
            results[name] = None
            return
        progress(f"Update: {name}...", None)
        try:
            r = fn()
        except Cancelled:
            raise
        except (HttpError, LookupError, RuntimeError, ValueError) as e:
            msg = f"{name}: stopped early ({e})"
            notes.append(msg)
            lines.append(msg)
            results[name] = None
            _log.warning("Update step %r stopped: %s", name, e)
            return
        results[name] = r
        lines.append(_step_line(name, r))

    igdb_ready = all(cfgmod.get(ctx.config, k) for k in IgdbApi.needs)
    steam_ready = bool(cfgmod.get(ctx.config, "steam.api_key"))

    # 1. bring in anything genuinely new first - this is the cheap, always-worthwhile part
    step("GOG catalog", lambda: build_from_records(conn, GogIngestor(ctx), progress), True)
    plats = cfgmod.get(ctx.config, "igdb.import_platforms") or ["pc"]
    plats = [p for p in plats if p in PLATFORM_IDS] or ["pc"]
    step("IGDB new games", lambda: build_from_records(conn, IgdbIngestor(ctx, plats), progress), igdb_ready)
    step("Steam new games", lambda: steam_catalog(conn, ctx, progress), steam_ready)

    if mode == "new_and_fill":
        # linking first means the size/price refresh right after has something to work with
        step("Match to Steam", lambda: steam_match(conn, ctx, progress), igdb_ready or steam_ready)
        ids = select_ids_all(conn)
        step("Fill in what's missing", lambda: refresh_fields(conn, ctx, ["all"], ids, None, progress, only_missing=True), True)
        step("Steam review scores", lambda: steamspy_scores(conn, ctx, progress), True)

    return {"mode": mode, "lines": lines, "notes": notes, "results": results}


def select_ids_all(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM releases")]
