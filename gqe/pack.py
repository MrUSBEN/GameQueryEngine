"""Fit games into limited space: a disk (one big bin) or discs (many equal bins).

rank:  'count'  -> every game is worth 1: fit as MANY games as possible
       'score'  -> worth 1 + review score: fit the BEST games
Strategies:
  optimal  exact-ish knapsack (dynamic programming, sizes rounded UP to `unit` so
           the result can never overflow). Bins are filled one after another.
  greedy   best value first, best-fit into bins; needed when you cap the number of games.
One release per game is kept (the smallest) so you don't get the same game twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field

try:
    import numpy as np
except ImportError:  # pure-Python fallback
    np = None


@dataclass
class Bin:
    capacity: int
    items: list[dict] = field(default_factory=list)

    @property
    def used(self) -> int:
        return sum(i["size_bytes"] for i in self.items)

    @property
    def free(self) -> int:
        return self.capacity - self.used


def _value(row: dict, rank: str, unknown_score: float) -> float:
    if rank == "count":
        return 1.0
    s = row.get("score")
    return 1.0 + (s if s is not None else unknown_score)


def _dedupe(rows: list[dict]) -> list[dict]:
    best: dict[int, dict] = {}
    for r in rows:
        cur = best.get(r["game_id"])
        if cur is None or r["size_bytes"] < cur["size_bytes"]:
            best[r["game_id"]] = r
    return list(best.values())


def _knapsack(items: list[tuple[int, float]], W: int) -> list[int]:
    """items: (weight_units, value). Returns indices chosen (0/1 knapsack)."""
    n = len(items)
    if np is not None:
        dp = np.zeros(W + 1)
        take = np.zeros((n, W + 1), dtype=bool)
        for i, (wt, v) in enumerate(items):
            if wt > W:
                continue
            cand = dp[: W + 1 - wt] + v
            better = cand > dp[wt:]
            take[i, wt:] = better
            dp[wt:] = np.where(better, cand, dp[wt:])
        chosen, w = [], W
        for i in range(n - 1, -1, -1):
            if take[i, w]:
                chosen.append(i)
                w -= items[i][0]
        return chosen
    dp = [0.0] * (W + 1)
    take = [bytearray(W + 1) for _ in range(n)]
    for i, (wt, v) in enumerate(items):
        if wt > W:
            continue
        for w in range(W, wt - 1, -1):
            c = dp[w - wt] + v
            if c > dp[w]:
                dp[w] = c
                take[i][w] = 1
    chosen, w = [], W
    for i in range(n - 1, -1, -1):
        if take[i][w]:
            chosen.append(i)
            w -= items[i][0]
    return chosen


def pack(rows: list[dict], bins: list[int], rank: str = "count", strategy: str = "optimal",
         max_items: int | None = None, unknown_score: float = 0.0,
         allow_duplicates: bool = False, unit: int = 10_000_000) -> dict:
    cands = [r for r in rows if r.get("size_bytes")]
    skipped_unknown = len(rows) - len(cands)
    if not allow_duplicates:
        cands = _dedupe(cands)
    cap_max = max(bins)
    cands = [r for r in cands if r["size_bytes"] <= cap_max]
    result = [Bin(c) for c in bins]

    if max_items or strategy == "greedy":
        order = sorted(cands, key=lambda r: (-_value(r, rank, unknown_score), r["size_bytes"]))
        placed = 0
        for r in order:
            if max_items and placed >= max_items:
                break
            fits = [b for b in result if b.free >= r["size_bytes"]]
            if fits:
                min(fits, key=lambda b: b.free).items.append(r)
                placed += 1
    else:
        remaining = list(cands)
        for b in result:
            u = max(unit, -(-b.capacity // 20000))  # keep DP table <= 20k columns
            W = b.capacity // u
            items = [(-(-r["size_bytes"] // u), _value(r, rank, unknown_score)) for r in remaining]
            picked = set(_knapsack(items, W))
            b.items = [remaining[i] for i in sorted(picked)]
            remaining = [r for i, r in enumerate(remaining) if i not in picked]

    return {
        "bins": result,
        "total_items": sum(len(b.items) for b in result),
        "total_bytes": sum(b.used for b in result),
        "candidates": len(cands),
        "skipped_unknown_size": skipped_unknown,
    }


def parse_bins(spec: str) -> list[int]:
    """'120gb' -> one bin;  '10x4.7gb' -> ten bins."""
    from .units import parse_size
    spec = spec.lower().replace(" ", "")
    if "x" in spec and spec.split("x", 1)[0].isdigit():
        n, size = spec.split("x", 1)
        return [parse_size(size)] * int(n)
    return [parse_size(spec)]
