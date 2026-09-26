"""Turn a Steam app's PICS record into an install size. Pure functions, standard library only.

PICS (Steam's Product Info system) lists an app's *depots* (content packages). SteamDB-style
"size on disk" = the sum of the depots a normal Windows install downloads:

  * only numeric depot ids (skips 'branches', 'baselanguages', ...)
  * only depots with a PUBLIC-branch manifest (the default install)
  * Windows or platform-neutral (no oslist)
  * English or language-neutral (skips other languages' voice/text packs)
  * not DLC depots (`dlcappid`/`optionaldlc`) and not low-violence variants
  * if both 32-bit and 64-bit depots exist, the 64-bit ones (they are alternatives, not additions)

Two record shapes exist in the wild: newer `manifests.public = {gid, size, download}` and older
`manifests.public = "<gid>"` with a depot-level `maxsize`. Both are handled and reported as `kind`.
Shared depots (`depotfromapp`, content lives under another app) carry no manifest here and are counted
as skipped, so games built mostly from shared depots may come out unsized rather than wrong.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _int(v):
    try:
        n = int(str(v).strip())
        return n if n >= 0 else None
    except (TypeError, ValueError):
        return None


def _cfg(depot: dict) -> dict:
    c = depot.get("config")
    return c if isinstance(c, dict) else {}


def original_year(app: dict) -> int | None:
    """Year of the ORIGINAL release if Steam records it (used to catch same-title different games)."""
    common = app.get("common") if isinstance(app.get("common"), dict) else {}
    ts = _int(common.get("original_release_date"))
    if not ts or ts < 315532800:            # before 1980 = junk
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).year


NON_GAME_TYPES = {"dlc", "demo", "music", "tool", "application", "video", "series", "advertising", "hardware",
                  "config", "beta", "mod"}


def app_type(app: dict) -> str | None:
    """Steam's own label for the app (Game, DLC, Demo, Music, Tool...) when it records one."""
    common = app.get("common") if isinstance(app.get("common"), dict) else {}
    t = str(common.get("type", "")).strip().lower()
    return t or None


def compute_install_size(app: dict) -> dict | None:
    depots = app.get("depots")
    if not isinstance(depots, dict):
        return None
    candidates = []
    for did, d in depots.items():
        if not str(did).isdigit() or not isinstance(d, dict):
            continue
        cfg = _cfg(d)
        oslist = str(cfg.get("oslist", "")).lower()
        if oslist and "windows" not in oslist:
            continue
        lang = str(cfg.get("language", "")).lower()
        if lang and lang != "english":
            continue
        if str(cfg.get("lowviolence", "")) == "1" or d.get("dlcappid") or d.get("optionaldlc"):
            continue
        candidates.append((did, d, cfg))
    have64 = any(str(c.get("osarch", "")) == "64" for _, _, c in candidates)
    size = download = used = skipped = 0
    kinds: set[str] = set()
    for did, d, cfg in candidates:
        arch = str(cfg.get("osarch", ""))
        if arch and arch != "64" and have64:
            continue
        manifests = d.get("manifests")
        public = manifests.get("public") if isinstance(manifests, dict) else None
        if public is None:
            skipped += 1
            continue
        if isinstance(public, dict):
            s, kind = _int(public.get("size")), "manifest"
            dl = _int(public.get("download"))
        else:
            s, kind, dl = _int(d.get("maxsize")), "maxsize", None
        if not s:
            skipped += 1
            continue
        size += s
        download += dl or 0
        used += 1
        kinds.add(kind)
    if not used:
        return None
    return {"size": size, "download": download or None, "depots": used, "skipped_depots": skipped,
            "kind": "manifest" if kinds == {"manifest"} else "maxsize", "original_year": original_year(app)}


# Steam's broad store genre ids (the ones every game carries). Anything else is ignored rather than guessed.
STEAM_GENRES = {"1": "action", "2": "strategy", "3": "rpg", "4": "casual", "9": "racing", "18": "sports",
                "23": "indie", "25": "adventure", "28": "simulation", "29": "mmo", "37": "free-to-play"}
# `common` keys worth counting in the census (which data Steam really returns to an anonymous login)
CENSUS_KEYS = ["name", "type", "oslist", "steam_release_date", "original_release_date", "metacritic_score",
               "review_score", "review_percentage", "genres", "store_tags", "category", "associations",
               "supported_languages"]


def genres_from_ids(ids) -> list[str]:
    return list(dict.fromkeys(STEAM_GENRES[str(i)] for i in ids if str(i) in STEAM_GENRES))


def app_summary(app: dict) -> dict:
    """The catalog-relevant facts from an app's PICS `common` block (values arrive as strings)."""
    c = app.get("common") if isinstance(app.get("common"), dict) else {}
    g = c.get("genres")
    oslist = str(c.get("oslist", "")).lower()
    return {"name": (c.get("name") or "").strip() or None, "type": app_type(app),
            "windows": (not oslist) or "windows" in oslist,
            "steam_release": _int(c.get("steam_release_date")), "original_release": _int(c.get("original_release_date")),
            "metacritic": _int(c.get("metacritic_score")), "review_percentage": _int(c.get("review_percentage")),
            "genre_ids": [str(v) for v in g.values()] if isinstance(g, dict) else [],
            "keys": sorted(c.keys())}
