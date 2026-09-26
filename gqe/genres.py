"""One vocabulary for genres across sources (IGDB, GOG, Steam disagree on spelling)."""
from __future__ import annotations

import re

_ALIASES = {
    "role-playing": "rpg", "role-playing (rpg)": "rpg", "role playing": "rpg",
    "real time strategy (rts)": "rts", "real-time strategy": "rts",
    "turn-based strategy (tbs)": "tbs",
    "hack and slash/beat 'em up": "beat-em-up", "beat 'em up": "beat-em-up",
    "shoot 'em up": "shmup", "shoot'em up": "shmup",
    "platform": "platformer", "simulator": "simulation", "sport": "sports",
    "quiz/trivia": "trivia", "card & board game": "card-board",
    "point-and-click": "point-and-click", "massively multiplayer online (mmo)": "mmo",
}


def norm_genre(name: str) -> str:
    key = re.sub(r"\s+", " ", str(name).strip().lower())
    return _ALIASES.get(key) or re.sub(r"[^a-z0-9]+", "-", key).strip("-")


def norm_genres(names) -> list[str]:
    return [g for g in dict.fromkeys(norm_genre(n) for n in names if n) if g]
