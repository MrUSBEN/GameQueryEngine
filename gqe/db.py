"""SQLite schema and connection.

Layout
  games       one row per game (cross-platform): name, original year, genres, score
  releases    one row per platform/region edition: size, drm, price, serial
  claims      every value any source ever told us (provenance); resolved values
              in games/releases are a cache computed from claims (see claims.py)
  source_link maps (source, source_key) -> our ids so re-running a build is idempotent
  user_state  your own favourites / played flags / notes
  saved_set   named result sets ("set=mylist" works as a filter)
  v_release   flat, query-friendly view: this is what filters run against
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from . import paths

SCHEMA_VERSION = 4   # bump when adding a migration below

TABLES = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS games(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL UNIQUE,
    original_year INTEGER,          -- true first-release year, any platform
    genres TEXT,                    -- '|rpg|action|' (pipe-delimited, lowercase)
    category TEXT,                  -- game / dlc / expansion / ...
    score REAL,                     -- normalised 0-100
    score_source TEXT
);

CREATE TABLE IF NOT EXISTS releases(
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id),
    platform TEXT NOT NULL,         -- slug: ps2, gc, pc ...
    region TEXT,
    release_date TEXT,              -- true date on THIS platform/region (ISO, may be YYYY only)
    serial TEXT,
    disc_count INTEGER DEFAULT 1,
    size_bytes INTEGER,
    size_source TEXT,
    size_conf TEXT,                 -- exact | depot | sysreq_estimate | media_estimate
    drm TEXT,                       -- drm-free | steam | denuvo | ...
    price_usd REAL,
    price_source TEXT,
    price_updated TEXT
);

CREATE TABLE IF NOT EXISTS source_link(
    source TEXT NOT NULL, source_key TEXT NOT NULL,
    game_id INTEGER, release_id INTEGER,
    PRIMARY KEY(source, source_key)
);

CREATE TABLE IF NOT EXISTS claims(
    id INTEGER PRIMARY KEY,
    target TEXT NOT NULL,           -- the TABLE name: 'games' | 'releases'
    row_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    source TEXT NOT NULL,
    value TEXT,
    confidence TEXT,
    fetched_at TEXT NOT NULL,
    UNIQUE(target, row_id, field, source)
);

CREATE TABLE IF NOT EXISTS user_state(
    game_id INTEGER PRIMARY KEY REFERENCES games(id),
    favorite INTEGER DEFAULT 0, played INTEGER DEFAULT 0, notes TEXT
);

CREATE TABLE IF NOT EXISTS saved_set(
    name TEXT NOT NULL, release_id INTEGER NOT NULL,
    PRIMARY KEY(name, release_id)
);

CREATE INDEX IF NOT EXISTS ix_rel_platform ON releases(platform);
CREATE INDEX IF NOT EXISTS ix_rel_size     ON releases(size_bytes);
CREATE INDEX IF NOT EXISTS ix_rel_game     ON releases(game_id);
CREATE INDEX IF NOT EXISTS ix_game_year    ON games(original_year);
CREATE INDEX IF NOT EXISTS ix_game_score   ON games(score);
CREATE INDEX IF NOT EXISTS ix_claim_target ON claims(target, row_id, field);
"""

# Dropped and recreated on every connect so schema tweaks never need a migration.
VIEW = """
DROP VIEW IF EXISTS v_release;
CREATE VIEW v_release AS
SELECT
    r.id                                   AS id,
    g.id                                   AS game_id,
    g.name                                 AS name,
    g.search_name                          AS search_name,
    r.platform                             AS platform,
    r.region                               AS region,
    r.release_date                         AS release_date,
    COALESCE(CAST(NULLIF(substr(r.release_date,1,4),'') AS INTEGER),
             g.original_year)              AS year,
    g.original_year                        AS orig_year,
    r.serial                               AS serial,
    r.disc_count                           AS discs,
    r.size_bytes                           AS size_bytes,
    ROUND(r.size_bytes / 1000000000.0, 3)  AS size_gb,
    r.size_source                          AS size_source,
    r.size_conf                            AS size_conf,
    g.genres                               AS genres,
    g.category                             AS category,
    g.score                                AS score,
    g.score_source                         AS score_source,
    r.drm                                  AS drm,
    r.price_usd                            AS price,
    r.price_source                         AS price_source,
    r.price_updated                        AS price_updated,
    COALESCE(u.favorite, 0)                AS fav,
    COALESCE(u.played, 0)                  AS played
FROM releases r
JOIN games g ON g.id = r.game_id
LEFT JOIN user_state u ON u.game_id = g.id;
"""


class DatabaseTooNew(RuntimeError):
    pass


# --- migrations -------------------------------------------------------------
# TABLES above is the v1 baseline. Every later change is a numbered step here, applied
# in order and in a transaction, ONLY after an automatic backup. Never edit a released
# step; add a new one. This is what lets a `git pull` upgrade an existing database
# in place instead of replacing it.
def _m2_source_state(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS source_state(
        source TEXT NOT NULL, key TEXT NOT NULL, value TEXT, updated_at TEXT,
        PRIMARY KEY(source, key))""")


def _m3_search_and_steam(conn):
    """Punctuation-insensitive title search, and a memory of Steam apps already checked (non-games etc.)."""
    conn.execute("ALTER TABLE games ADD COLUMN search_name TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS steam_checked(
        appid INTEGER PRIMARY KEY, kind TEXT, checked_at TEXT)""")
    from .ingest import search_norm
    rows = conn.execute("SELECT id, name FROM games").fetchall()
    conn.executemany("UPDATE games SET search_name=? WHERE id=?", [(search_norm(n), i) for i, n in rows])


def _m4_activity_log(conn):
    """A running record of every source run / import / refresh / backup, independent of
    whether it wrote any claims. This is what lets the Status tab tell 'recently updated'
    (new/changed values) apart from 'recently checked' (ran, nothing new)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS activity_log(
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,          -- source | library | refresh | import | csv | restore | backup
        source TEXT NOT NULL,        -- e.g. 'steam_catalog', 'update_library:new', 'redump', 'csv'
        platforms TEXT,              -- JSON list of platform slugs touched, or NULL = all/unknown
        ok INTEGER NOT NULL,
        summary TEXT,
        detail TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_activity_at ON activity_log(at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_activity_source ON activity_log(source)")


MIGRATIONS = {2: _m2_source_state, 3: _m3_search_and_steam, 4: _m4_activity_log}


def _get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))


def backup_database(conn: sqlite3.Connection, path: Path, version: int, keep: int = 5) -> Path:
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{path.stem}-v{version}-{time.strftime('%Y%m%d-%H%M%S')}.db"
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
    finally:
        out.close()
    for old in sorted(folder.glob(f"{path.stem}-v*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return dest


def _migrate(conn: sqlite3.Connection, path: Path | str) -> None:
    current = _get_meta(conn, "schema_version")
    fresh = current is None
    version = 1 if fresh else int(current)
    if version > SCHEMA_VERSION:
        raise DatabaseTooNew(
            f"This database was created by a newer version (schema {version}, this app knows {SCHEMA_VERSION}). "
            "Update the app instead of downgrading; your data was not touched.")
    if version < SCHEMA_VERSION:
        has_data = not fresh and conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] > 0
        if has_data and str(path) != ":memory:":
            conn.commit()
            backup_database(conn, Path(path), version)
        for v in range(version + 1, SCHEMA_VERSION + 1):
            MIGRATIONS[v](conn)
            _set_meta(conn, "schema_version", v)
        conn.commit()
    elif fresh:
        _set_meta(conn, "schema_version", SCHEMA_VERSION)
        conn.commit()


def _refresh_resolved_if_rules_changed(conn: sqlite3.Connection) -> None:
    """Source-ranking rules live in code. If a `git pull` changed them, recompute the
    resolved values from the stored claims (no network, nothing lost)."""
    from . import claims
    sig = hashlib.sha1(json.dumps([claims.PRECEDENCE, sorted(claims.LATEST_WINS)],
                                  sort_keys=True).encode()).hexdigest()
    if _get_meta(conn, "rules_hash") != sig:
        if conn.execute("SELECT 1 FROM claims LIMIT 1").fetchone():
            claims.re_resolve_all(conn)
        _set_meta(conn, "rules_hash", sig)
        conn.commit()


def default_db_path() -> Path:
    return paths.db_path()


def connect(path: str | Path | None = None, init: bool = True) -> sqlite3.Connection:
    path = Path(path) if path else default_db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    if init:
        conn.executescript(TABLES)
        _migrate(conn, path)
        conn.executescript(VIEW)
        _refresh_resolved_if_rules_changed(conn)
    return conn
