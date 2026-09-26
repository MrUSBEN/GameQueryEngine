"""Dynamic filter language -> parameterised SQL against v_release.

    year=2005                     size<2gb              score>=60%
    year=2003..2006               size=500mb..2gb       platform=ps2,gc
    genre:rpg,action              name~"resident evil"  price<=10
    size=unknown                  size!=unknown         fav=yes
    set=mylist                    id=12,15,99           size_conf=exact

Operators:  =  !=  <  <=  >  >=  ~ (contains)  !~ (does not contain)  : (has any of)
Word forms: lt lte gt gte eq ne has in     ->   size lt 2gb   year gte 2003
Spaces around operators are fine:               size >= 2gb

Numeric fields: unknown (NULL) values never match a comparison; ask for them with
`field=unknown`, or exclude them with `field!=unknown`.
Text/list fields: the NEGATIVE forms (!=, !~) keep unknown values, because "not PC" or
"not indie" should still show games where that field is empty.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Iterable

from .ingest import search_norm
from .platforms import normalize_platform
from .units import parse_size


class FilterError(ValueError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    column: str
    kind: str  # text | int | num | size | list | bool | set


FIELDS: dict[str, FieldSpec] = {
    "id": FieldSpec("id", "int"),
    "name": FieldSpec("name", "text"),
    "platform": FieldSpec("platform", "text"),
    "region": FieldSpec("region", "text"),
    "category": FieldSpec("category", "text"),
    "drm": FieldSpec("drm", "text"),
    "serial": FieldSpec("serial", "text"),
    "size_conf": FieldSpec("size_conf", "text"),
    "size_source": FieldSpec("size_source", "text"),
    "score_source": FieldSpec("score_source", "text"),
    "price_source": FieldSpec("price_source", "text"),
    "year": FieldSpec("year", "int"),
    "orig_year": FieldSpec("orig_year", "int"),
    "discs": FieldSpec("discs", "int"),
    "size": FieldSpec("size_bytes", "size"),
    "score": FieldSpec("score", "num"),
    "price": FieldSpec("price", "num"),
    "genre": FieldSpec("genres", "list"),
    "fav": FieldSpec("fav", "bool"),
    "played": FieldSpec("played", "bool"),
    "set": FieldSpec("id", "set"),
}
FIELD_ALIASES = {
    "title": "name", "console": "platform", "system": "platform", "genres": "genre",
    "rating": "score", "review": "score", "reviews": "score", "cost": "price",
    "favorite": "fav", "favourite": "fav", "size_bytes": "size", "yr": "year",
}

_WORD_OPS = {"nhas": "!~", "lt": "<", "lte": "<=", "le": "<=", "gt": ">", "gte": ">=", "ge": ">=",
             "eq": "=", "ne": "!=", "has": "~", "in": ":"}
_OP_RE = r"(?:<=|>=|!=|!~|=|<|>|~|:)"
_STARTS_OP = re.compile(rf"^{_OP_RE}")
_ENDS_OP = re.compile(rf"{_OP_RE}$")
_CLAUSE_RE = re.compile(rf"^([A-Za-z_]+)\s*({_OP_RE})\s*(.+)$", re.S)
_UNKNOWN = {"unknown", "null", "none", "?"}


@dataclass(frozen=True)
class Clause:
    field: str
    op: str
    value: str
    or_unknown: bool = False   # UI "also include games where this is unknown"

    def __str__(self) -> str:
        return f"{self.field}{self.op}{self.value}"


def canonical_field(name: str) -> str:
    key = name.strip().lower()
    key = FIELD_ALIASES.get(key, key)
    if key not in FIELDS:
        raise FilterError(f"unknown field {name!r}. Fields: {', '.join(sorted(FIELDS))}")
    return key


def _merge_tokens(toks: list[str]) -> list[str]:
    out, i, n = [], 0, len(toks)
    while i < n:
        t = toks[i]
        i += 1
        if re.fullmatch(r"[A-Za-z_]+", t) and i < n:
            nxt = toks[i]
            if nxt.lower() in _WORD_OPS:
                t += _WORD_OPS[nxt.lower()]
                i += 1
            elif _STARTS_OP.match(nxt):
                t += nxt
                i += 1
        if _ENDS_OP.search(t) and i < n:  # dangling operator: value is next token
            t += toks[i]
            i += 1
        out.append(t)
    return out


def parse_filters(parts: Iterable[str]) -> list[Clause]:
    toks: list[str] = []
    for p in parts:
        try:
            toks.extend(shlex.split(p))
        except ValueError as e:
            raise FilterError(f"bad quoting in {p!r}: {e}") from None
    clauses = []
    for tok in _merge_tokens(toks):
        m = _CLAUSE_RE.match(tok)
        if not m:
            raise FilterError(f"cannot parse filter {tok!r} (expected e.g. size<2gb)")
        field = canonical_field(m.group(1))
        clauses.append(Clause(field, m.group(2), m.group(3).strip()))
    return clauses


# ---------------------------------------------------------------- SQL building
def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _to_int(s: str) -> int:
    try:
        return int(s.strip())
    except ValueError:
        raise FilterError(f"not an integer: {s!r}") from None


def _to_num(s: str) -> float:
    try:
        return float(s.strip().rstrip("%").lstrip("$"))
    except ValueError:
        raise FilterError(f"not a number: {s!r}") from None


def _to_size(s: str) -> int:
    try:
        return parse_size(s.strip())
    except ValueError as e:
        raise FilterError(str(e)) from None


_CONV = {"int": _to_int, "num": _to_num, "size": _to_size}


def _numeric(col: str, kind: str, op: str, val: str):
    conv = _CONV[kind]
    if op in ("<", "<=", ">", ">="):
        return f"{col} {op} ?", [conv(val)]
    if op in ("=", "!="):
        if op == "=" and ".." in val:
            lo, hi = val.split("..", 1)
            parts, params = [], []
            if lo.strip():
                parts.append(f"{col} >= ?")
                params.append(conv(lo))
            if hi.strip():
                parts.append(f"{col} <= ?")
                params.append(conv(hi))
            if not parts:
                raise FilterError(f"empty range: {val!r}")
            return "(" + " AND ".join(parts) + ")", params
        vals = [conv(v) for v in val.split(",") if v.strip()]
        if len(vals) == 1:
            return f"{col} {op} ?", vals
        neg = "NOT " if op == "!=" else ""
        return f"{col} {neg}IN ({','.join('?' * len(vals))})", vals
    raise FilterError(f"operator {op!r} does not apply to numeric fields")


def _text(col: str, op: str, val: str):
    vals = [v.strip() for v in val.split(",") if v.strip()]
    if not vals:
        raise FilterError("empty value")
    if op in ("=", "!="):
        if col == "platform":
            vals = [normalize_platform(v) for v in vals]
        marks = ",".join("?" * len(vals))
        if op == "!=":
            return f"({col} IS NULL OR LOWER({col}) NOT IN ({marks}))", [v.lower() for v in vals]
        return f"LOWER({col}) IN ({marks})", [v.lower() for v in vals]
    if col == "name" and op in ("~", "!~"):
        # match the title as typed OR with punctuation ignored, so "ark survival" finds "ARK: Survival Evolved"
        conds, params = [], []
        for v in vals:
            if re.fullmatch(r"[A-Za-z0-9 ]+", v):        # typed no punctuation: also match titles that have some
                conds.append("(name LIKE ? ESCAPE '\\' OR COALESCE(search_name, '') LIKE ? ESCAPE '\\')")
                params += [f"%{_esc(v)}%", f"%{_esc(search_norm(v))}%"]
            else:                                          # typed punctuation on purpose: exactly what was typed
                conds.append("name LIKE ? ESCAPE '\\'")
                params.append(f"%{_esc(v)}%")
        joined = " OR ".join(conds)
        return (f"({joined})" if op == "~" else f"(NOT ({joined}))"), params
    if op in ("~", ":"):
        return ("(" + " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for _ in vals) + ")",
                [f"%{_esc(v)}%" for v in vals])
    if op == "!~":
        return ("(" + f"{col} IS NULL OR (" + " AND ".join(f"{col} NOT LIKE ? ESCAPE '\\'" for _ in vals) + "))",
                [f"%{_esc(v)}%" for v in vals])
    raise FilterError(f"operator {op!r} does not apply to text fields")


def _list(col: str, op: str, val: str):
    vals = [v.strip().lower() for v in val.split(",") if v.strip()]
    if not vals:
        raise FilterError("empty value")
    if op in ("~", "!~"):
        pats = [f"%{_esc(v)}%" for v in vals]
    elif op in ("=", ":", "!="):
        pats = [f"%|{_esc(v)}|%" for v in vals]
    else:
        raise FilterError(f"operator {op!r} does not apply to list fields")
    any_sql = " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for _ in pats)
    if op in ("!=", "!~"):
        return f"({col} IS NULL OR NOT ({any_sql}))", pats
    return f"({any_sql})", pats


def _bool(col: str, op: str, val: str):
    v = val.strip().lower()
    if v in {"1", "yes", "y", "true", "t", "on"}:
        target = 1
    elif v in {"0", "no", "n", "false", "f", "off"}:
        target = 0
    else:
        raise FilterError(f"expected yes/no, got {val!r}")
    if op not in ("=", "!="):
        raise FilterError("yes/no fields only support = and !=")
    return f"{col} {op} ?", [target]


def clause_sql(c: Clause):
    spec = FIELDS[c.field]
    col, kind, op, val = spec.column, spec.kind, c.op, c.value
    if kind == "set":
        if op not in ("=", ":"):
            raise FilterError("set only supports = (e.g. set=mylist)")
        return "id IN (SELECT release_id FROM saved_set WHERE name = ?)", [val]
    if val.lower() in _UNKNOWN:
        if op == "=":
            return f"{col} IS NULL", []
        if op == "!=":
            return f"{col} IS NOT NULL", []
        raise FilterError("use =unknown or !=unknown")
    if kind in _CONV:
        return _numeric(col, kind, op, val)
    if kind == "text":
        return _text(col, op, val)
    if kind == "list":
        return _list(col, op, val)
    return _bool(col, op, val)


def compile_clauses(clauses: Iterable[Clause]):
    parts, params = [], []
    for c in clauses:
        sql, p = clause_sql(c)
        if c.or_unknown and FIELDS[c.field].kind not in ("set", "bool"):
            sql = f"({sql} OR {FIELDS[c.field].column} IS NULL)"
        parts.append(f"({sql})")
        params.extend(p)
    return (" AND ".join(parts) or "1=1"), params


def build_order(sort: str | None) -> str:
    """'score-,size' / 'name' / '-year'. Suffix or prefix '-' = descending. NULLs last."""
    if not sort:
        return " ORDER BY name COLLATE NOCASE, platform"
    parts = []
    for tok in sort.split(","):
        tok = tok.strip()
        if not tok:
            continue
        desc = False
        if tok.startswith("-"):
            desc, tok = True, tok[1:]
        elif tok.endswith("-"):
            desc, tok = True, tok[:-1]
        elif tok.endswith("+"):
            tok = tok[:-1]
        spec = FIELDS[canonical_field(tok)]
        if spec.kind == "set":
            raise FilterError("cannot sort by set")
        collate = " COLLATE NOCASE" if spec.kind in ("text", "list") else ""
        parts.append(f"{spec.column} IS NULL, {spec.column}{collate} {'DESC' if desc else 'ASC'}")
    return " ORDER BY " + ", ".join(parts) if parts else ""
