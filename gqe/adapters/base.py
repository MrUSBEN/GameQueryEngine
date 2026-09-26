"""Adapter contracts.

Ingestor  - bulk source used to BUILD/UPDATE the database (creates releases).
Refresher - per-item lookup used by "refresh this column for these games" (adds claims).

Adapters never touch the database directly: they receive a Context (HTTP client, config,
progress callback, small key/value state) and return Records / Updates.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from .. import config as cfgmod
from ..claims import now_iso
from ..http import Http, RateLimiter  # noqa: F401  (RateLimiter re-exported)

Progress = Callable[[str, float | None], None]


def _noop(msg: str, frac: float | None = None) -> None:
    pass


@dataclass
class Context:
    conn: object
    http: Http
    config: dict = field(default_factory=dict)
    progress: Progress = _noop
    services: dict = field(default_factory=dict)   # injectable helpers (tests replace subprocess runners here)

    def state_get(self, source: str, key: str):
        row = self.conn.execute("SELECT value FROM source_state WHERE source=? AND key=?",
                                (source, key)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def state_set(self, source: str, key: str, value) -> None:
        self.conn.execute(
            """INSERT INTO source_state(source,key,value,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(source,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (source, key, json.dumps(value), now_iso()))
        self.conn.commit()


@dataclass
class Record:
    source: str
    source_key: str            # stable id within the source (makes re-builds idempotent)
    name: str
    platform: str
    region: str | None = None
    size_bytes: int | None = None
    size_conf: str | None = None
    discs: int = 1
    serial: str | None = None
    release_date: str | None = None
    claims: list = field(default_factory=list)   # [(field, value, confidence|None)]
    aliases: list = field(default_factory=list)  # other names, used to find an existing entry


@dataclass
class Update:
    release_id: int
    field: str                 # a key of claims.FIELD_MAP, or "link" (value = the source's own id)
    value: object
    confidence: str | None = None


class Ingestor(ABC):
    name: str

    @abstractmethod
    def records(self) -> Iterator[Record]: ...


class Refresher(ABC):
    name: str
    fields: frozenset = frozenset()
    needs: tuple = ()          # dotted config keys that must be set, e.g. ("igdb.client_id",)
    link_source: str | None = None         # which source_link rows hold this source's ids (default: its own name)
    manual_only: frozenset = frozenset()   # fields this source fills ONLY when the user picks it explicitly
                                           # (slow methods that must not run under "Automatic")

    def __init__(self, ctx: Context):
        self.ctx = ctx

    def missing_config(self) -> list[str]:
        return [k for k in self.needs if not cfgmod.get(self.ctx.config, k)]

    def unavailable(self) -> str | None:
        """Reason this source can't run right now (e.g. an optional add-on isn't installed)."""
        return None

    def supports(self, row: dict) -> bool:
        return True

    def estimate(self, rows: list[dict], fields: list[str], links: dict) -> float:
        """Rough seconds this would take (shown to the user before they start)."""
        return 0.0

    @abstractmethod
    def fetch(self, rows: list[dict], fields: list[str], links: dict) -> Iterable[Update]: ...
