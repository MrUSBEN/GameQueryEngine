"""Platform slugs. Everything in the DB uses the slug (ps2, gc, pc, ...)."""
from __future__ import annotations

import re

_GROUPS = {
    "pc": ["pc", "windows", "win", "steam", "gog"],
    "ps1": ["ps1", "psx", "psone", "ps one", "playstation", "sony - playstation", "sony playstation"],
    "ps2": ["ps2", "playstation 2", "playstation2", "sony - playstation 2", "sony playstation 2"],
    "ps3": ["ps3", "playstation 3", "sony - playstation 3"],
    "psp": ["psp", "playstation portable", "sony - playstation portable"],
    "gc": ["gc", "gcn", "ngc", "gamecube", "game cube", "nintendo - gamecube"],
    "wii": ["wii", "nintendo - wii"],
    "xbox": ["xbox", "og xbox", "microsoft - xbox"],
    "x360": ["x360", "xbox 360", "xbox360", "microsoft - xbox 360"],
    "dc": ["dc", "dreamcast", "sega - dreamcast"],
    "saturn": ["saturn", "sega saturn", "sega - saturn"],
}
_LOOKUP = {alias: slug for slug, aliases in _GROUPS.items() for alias in aliases}


def normalize_platform(text: str) -> str:
    key = re.sub(r"\s+", " ", text.strip().lower())
    return _LOOKUP.get(key) or re.sub(r"[^a-z0-9]+", "", key)
