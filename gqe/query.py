from __future__ import annotations

import sqlite3
from typing import Iterable, Sequence

from .filters import Clause, build_order, compile_clauses, parse_filters


def select(conn: sqlite3.Connection, clauses: Sequence[Clause] | Iterable[str] = (),
           sort: str | None = None, limit: int | None = None, offset: int = 0) -> list[dict]:
    clauses = _as_clauses(clauses)
    where, params = compile_clauses(clauses)
    sql = f"SELECT * FROM v_release WHERE {where}{build_order(sort)}"
    if limit:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    return [dict(r) for r in conn.execute(sql, params)]


def ids(conn: sqlite3.Connection, clauses=()) -> list[int]:
    """Just the release ids (much lighter than select() for 100k+ rows)."""
    where, params = compile_clauses(_as_clauses(clauses))
    return [r[0] for r in conn.execute(f"SELECT id FROM v_release WHERE {where}", params)]


def count(conn: sqlite3.Connection, clauses=()) -> int:
    where, params = compile_clauses(_as_clauses(clauses))
    return conn.execute(f"SELECT COUNT(*) FROM v_release WHERE {where}", params).fetchone()[0]


def _as_clauses(clauses) -> list[Clause]:
    clauses = list(clauses)
    if clauses and isinstance(clauses[0], str):
        return parse_filters(clauses)
    return clauses


def stats(conn: sqlite3.Connection) -> list[dict]:
    """Per-platform data coverage: how complete is each column?"""
    sql = """
    SELECT platform,
           COUNT(*) AS releases,
           ROUND(100.0 * SUM(size_bytes IS NOT NULL) / COUNT(*), 1) AS size_pct,
           ROUND(100.0 * SUM(size_conf = 'exact')   / COUNT(*), 1) AS size_exact_pct,
           ROUND(100.0 * SUM(year IS NOT NULL)      / COUNT(*), 1) AS year_pct,
           ROUND(100.0 * SUM(score IS NOT NULL)     / COUNT(*), 1) AS score_pct,
           ROUND(100.0 * SUM(genres IS NOT NULL)    / COUNT(*), 1) AS genre_pct,
           ROUND(100.0 * SUM(price IS NOT NULL)     / COUNT(*), 1) AS price_pct,
           ROUND(100.0 * SUM(drm IS NOT NULL)       / COUNT(*), 1) AS drm_pct
    FROM v_release GROUP BY platform ORDER BY releases DESC
    """
    return [dict(r) for r in conn.execute(sql)]
