"""Export a list of rows at a chosen detail level. Pure functions, no I/O side effects
except the returned text/bytes, so the UI can stream them as a download."""
from __future__ import annotations

import csv
import io
import json

DETAIL = {
    "minimal":  ["name", "platform", "year", "size_gb"],
    "standard": ["name", "platform", "region", "year", "size_gb", "size_conf",
                 "genres", "score", "price", "drm"],
    "full":     ["id", "name", "platform", "region", "release_date", "year", "orig_year",
                 "serial", "discs", "size_bytes", "size_gb", "size_conf", "size_source",
                 "genres", "category", "score", "score_source", "price", "price_source", "price_updated", "drm"],
}
ALL_COLUMNS = DETAIL["full"]
LABELS = {"size_gb": "Size (GB)", "size_bytes": "Size (bytes)", "orig_year": "Original year",
          "release_date": "Release date", "size_conf": "Size accuracy", "size_source": "Size source",
          "price_source": "Price source", "score_source": "Score source", "price_updated": "Price updated", "id": "ID",
          "name": "Name", "platform": "Platform", "region": "Region", "year": "Year",
          "serial": "Serial", "discs": "Discs", "genres": "Genres", "category": "Category",
          "score": "Score", "price": "Price ($)", "drm": "DRM"}
FORMATS = {"csv": "text/csv", "json": "application/json", "md": "text/markdown", "txt": "text/plain"}


def _plain(col: str, v):
    """Raw-but-tidy value for csv/json."""
    if v is None:
        return None
    if col == "genres":
        return "; ".join(p for p in str(v).split("|") if p)
    return v


def _pretty(col: str, v) -> str:
    """Display value for md/txt tables."""
    if v is None or v == "":
        return "-"
    if col == "size_gb":
        return f"{v:.2f}"
    if col == "score":
        return f"{v:.0f}"
    if col == "price":
        return f"{v:.2f}"
    return str(_plain(col, v))


def columns_for(detail: str = "standard", columns: list[str] | None = None) -> list[str]:
    if columns:
        bad = [c for c in columns if c not in ALL_COLUMNS]
        if bad:
            raise ValueError(f"unknown columns: {', '.join(bad)}")
        return columns
    if detail not in DETAIL:
        raise ValueError(f"detail must be one of {', '.join(DETAIL)}")
    return DETAIL[detail]


def render(rows: list[dict], fmt: str = "csv", detail: str = "standard",
           columns: list[str] | None = None) -> str:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    cols = columns_for(detail, columns)
    if fmt == "json":
        return json.dumps([{c: _plain(c, r.get(c)) for c in cols} for r in rows],
                          indent=2, ensure_ascii=False)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if _plain(c, r.get(c)) is None else _plain(c, r.get(c)) for c in cols])
        return buf.getvalue()
    table = [[_pretty(c, r.get(c)) for c in cols] for r in rows]
    heads = [LABELS.get(c, c) for c in cols]
    if fmt == "md":
        esc = lambda s: s.replace("|", "\\|")
        lines = ["| " + " | ".join(heads) + " |", "|" + "|".join("---" for _ in cols) + "|"]
        lines += ["| " + " | ".join(esc(x) for x in row) + " |" for row in table]
        return "\n".join(lines) + "\n"
    widths = [max(len(h), *(len(row[i]) for row in table)) if table else len(h)
              for i, h in enumerate(heads)]
    fmt_row = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()
    return "\n".join([fmt_row(heads), fmt_row(["-" * w for w in widths])] + [fmt_row(r) for r in table]) + "\n"
