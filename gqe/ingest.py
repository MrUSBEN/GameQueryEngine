"""Writing source records into the database (idempotent)."""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import unicodedata

from .claims import FIELD_MAP, record_claim
from .platforms import normalize_platform


def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", s)


def search_norm(name: str) -> str:
    """Lower-case words separated by single spaces, punctuation gone: 'ARK: Survival Evolved' -> 'ark survival evolved'.
    What title search compares against, so 'ark survival' finds it."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def upsert_release(conn: sqlite3.Connection, *, source: str, source_key: str, name: str,
                   platform: str, region: str | None = None, size_bytes: int | None = None,
                   size_conf: str | None = None, discs: int = 1, serial: str | None = None,
                   release_date: str | None = None, name_norm: str | None = None) -> int:
    """Create/update a release from a source record. Same (source, source_key) -> same row."""
    platform = normalize_platform(platform)
    link = conn.execute("SELECT release_id FROM source_link WHERE source=? AND source_key=?",
                        (source, source_key)).fetchone()
    if link:
        rid = link[0]
        conn.execute("UPDATE releases SET platform=?, region=?, serial=?, disc_count=? WHERE id=?",
                     (platform, region, serial, discs, rid))
    else:
        nn = name_norm or norm_name(name)      # callers pass a disambiguated key for same-title different games
        g = conn.execute("SELECT id FROM games WHERE name_norm=?", (nn,)).fetchone()
        gid = g[0] if g else conn.execute(
            "INSERT INTO games(name, name_norm, search_name) VALUES(?,?,?)", (name, nn, search_norm(name))).lastrowid
        rid = conn.execute(
            "INSERT INTO releases(game_id, platform, region, serial, disc_count) VALUES(?,?,?,?,?)",
            (gid, platform, region, serial, discs)).lastrowid
        conn.execute("INSERT INTO source_link VALUES(?,?,?,?)", (source, source_key, gid, rid))
    if size_bytes:
        record_claim(conn, rid, "size", source, size_bytes, size_conf)
    if release_date:
        record_claim(conn, rid, "release_date", source, release_date)
    return rid


def import_csv_text(conn: sqlite3.Connection, text: str, source: str = "manual") -> dict:
    """CSV with an `id` column (release id, as shown in the table) plus any of the
    fields: size, price, drm, release_date, orig_year, score, genres, category."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "id" not in [f.strip().lower() for f in reader.fieldnames]:
        raise ValueError("CSV needs an 'id' column")
    applied = skipped = 0
    errors: list[str] = []
    for line, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): v for k, v in row.items()}
        try:
            rid = int(row["id"])
        except (KeyError, ValueError, TypeError):
            errors.append(f"line {line}: bad id")
            continue
        for field, value in row.items():
            if field in FIELD_MAP and value and value.strip():
                try:
                    record_claim(conn, rid, field, source, value.strip())
                    applied += 1
                except (KeyError, ValueError) as e:
                    skipped += 1
                    errors.append(f"line {line} {field}: {e}")
    conn.commit()
    return {"applied": applied, "skipped": skipped, "errors": errors[:20]}
