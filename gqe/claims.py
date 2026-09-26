"""Provenance layer.

Every value any source tells us is stored as a *claim* (source + time + optional
confidence). The value shown in the table is *resolved* from the claims:

    manual (you)  >  per-field source ranking (PRECEDENCE)  >  newest

Price is the exception: the newest claim wins (LATEST_WINS), because a fresh
price beats an old one regardless of source. Re-resolving never needs the network.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .units import parse_size


@dataclass(frozen=True)
class Target:
    table: str
    column: str
    kind: str                     # int | real | text | list
    source_col: str | None = None
    conf_col: str | None = None
    ts_col: str | None = None


FIELD_MAP: dict[str, Target] = {
    "size":         Target("releases", "size_bytes", "int", "size_source", "size_conf"),
    "price":        Target("releases", "price_usd", "real", "price_source", None, "price_updated"),
    "drm":          Target("releases", "drm", "text"),
    "release_date": Target("releases", "release_date", "text"),
    "orig_year":    Target("games", "original_year", "int"),
    "score":        Target("games", "score", "real", "score_source"),
    "genres":       Target("games", "genres", "list"),
    "category":     Target("games", "category", "text"),
}

PRECEDENCE: dict[str, list[str]] = {
    "size": ["gog", "gogdb", "redump", "steam_pics", "steamcmd", "steam_sysreq", "estimate"],
    "release_date": ["mobygames", "igdb", "rawg", "wikidata", "gog", "gogdb", "steam"],
    "orig_year": ["mobygames", "igdb", "rawg", "wikidata", "gog", "steam"],
    "score": ["igdb", "mobygames", "rawg", "steam", "gog", "steamspy"],
    "genres": ["igdb", "mobygames", "rawg", "gog", "steam", "steamspy"],
    "drm": ["pcgamingwiki", "gog", "gogdb", "steam"],
    "category": ["igdb", "gog", "gogdb", "steam"],
}
LATEST_WINS = {"price"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()   # microseconds: two claims in one second must still order


def normalize_value(field: str, value) -> str:
    kind = FIELD_MAP[field].kind
    if kind == "int":
        if field == "size" and isinstance(value, str) and not value.strip().isdigit():
            return str(parse_size(value))
        return str(int(float(value)))
    if kind == "real":
        return str(float(str(value).lstrip("$").rstrip("%")))
    if kind == "list":
        items = value.split(",") if isinstance(value, str) else list(value)
        items = [str(i).strip().lower() for i in items if str(i).strip()]
        return "|" + "|".join(dict.fromkeys(items)) + "|" if items else ""
    return str(value).strip()


def _from_store(kind: str, text: str):
    if kind == "int":
        return int(text)
    if kind == "real":
        return float(text)
    return text


def _rank(field: str, source: str) -> int:
    if source == "manual":
        return -1
    if field in LATEST_WINS:
        return 0
    order = PRECEDENCE.get(field, [])
    return order.index(source) if source in order else len(order)


def _row_id_for(conn, target: Target, release_id: int) -> int:
    row = conn.execute("SELECT game_id FROM releases WHERE id=?", (release_id,)).fetchone()
    if row is None:
        raise KeyError(f"no game with id {release_id}")
    return release_id if target.table == "releases" else row[0]


def record_claim(conn: sqlite3.Connection, release_id: int, field: str, source: str,
                 value, confidence: str | None = None, fetched_at: str | None = None) -> None:
    """Store a claim for the release (or its game, for game-level fields) and re-resolve."""
    if field not in FIELD_MAP:
        raise KeyError(f"unknown field {field!r}. Known: {', '.join(FIELD_MAP)}")
    if value is None or str(value).strip() == "":
        return
    target = FIELD_MAP[field]
    row_id = _row_id_for(conn, target, release_id)
    stored = normalize_value(field, value)
    if stored == "":
        return
    conn.execute(
        """INSERT INTO claims(target, row_id, field, source, value, confidence, fetched_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(target,row_id,field,source) DO UPDATE SET
             value=excluded.value, confidence=excluded.confidence, fetched_at=excluded.fetched_at""",
        (target.table, row_id, field, source, stored,
         confidence or ("manual" if source == "manual" else None), fetched_at or now_iso()))
    resolve(conn, field, row_id)


def resolve(conn: sqlite3.Connection, field: str, row_id: int) -> None:
    t = FIELD_MAP[field]
    claims = conn.execute(
        "SELECT * FROM claims WHERE target=? AND row_id=? AND field=? ORDER BY fetched_at DESC, id DESC",
        (t.table, row_id, field)).fetchall()
    sets, params = [f"{t.column}=?"], []
    if claims:
        best = min(claims, key=lambda c: _rank(field, c["source"]))  # stable: newest wins ties
        params.append(_from_store(t.kind, best["value"]))
        for col, val in ((t.source_col, best["source"]), (t.conf_col, best["confidence"]),
                         (t.ts_col, best["fetched_at"])):
            if col:
                sets.append(f"{col}=?")
                params.append(val)
    else:
        params.append(None)
        for col in (t.source_col, t.conf_col, t.ts_col):
            if col:
                sets.append(f"{col}=?")
                params.append(None)
    params.append(row_id)
    conn.execute(f"UPDATE {t.table} SET {', '.join(sets)} WHERE id=?", params)


def re_resolve_all(conn: sqlite3.Connection) -> int:
    """Recompute every resolved value from claims (after changing PRECEDENCE)."""
    n = 0
    for c in conn.execute("SELECT DISTINCT field, row_id FROM claims").fetchall():
        resolve(conn, c["field"], c["row_id"])
        n += 1
    conn.commit()
    return n
