"""Size parsing/formatting.

GB/MB are DECIMAL (10^9 / 10^6) by default because that is how disc media is
rated: a "4.7 GB" DVD holds 4.7e9 bytes (= ~4.38 GiB). Use GiB/MiB for binary.
"""
from __future__ import annotations

import re

_UNITS = {
    "b": 1, "kb": 10**3, "mb": 10**6, "gb": 10**9, "tb": 10**12,
    "kib": 2**10, "mib": 2**20, "gib": 2**30, "tib": 2**40,
}
_ALIAS = {"k": "kb", "m": "mb", "g": "gb", "t": "tb",
          "ki": "kib", "mi": "mib", "gi": "gib", "ti": "tib",
          "byte": "b", "bytes": "b"}
_RE = re.compile(r"^\s*(\d+(?:\.\d+)?|\.\d+)\s*([a-z]*)\s*$", re.I)


def parse_size(text, default_unit: str = "gb") -> int:
    """'2gb', '1.5 GBs', '700mb', '4.38GiB', '2' (-> default unit) -> bytes."""
    m = _RE.match(str(text))
    if not m:
        raise ValueError(f"cannot parse size: {text!r}")
    number, unit = float(m.group(1)), m.group(2).lower() or default_unit
    for cand in (unit, unit[:-1] if unit.endswith("s") else unit):
        cand = _ALIAS.get(cand, cand)
        if cand in _UNITS:
            return int(round(number * _UNITS[cand]))
    raise ValueError(f"unknown size unit {unit!r} in {text!r}")


def format_size(n) -> str:
    if n is None:
        return "?"
    for unit, div in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"
