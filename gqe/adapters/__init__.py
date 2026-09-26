from __future__ import annotations

from .base import Context, Ingestor, Record, Refresher, Update  # noqa: F401
from .gog import GogIngestor, GogSizeRefresher
from .igdb import IgdbRefresher
from .rawg import RawgRefresher
from .redump import RedumpIngestor
from .steam import SteamRefresher
from . import steam_pics  # noqa: F401
from .steam_pics import SteamPicsRefresher
from .wikidata import WikidataRefresher

INGESTORS = {"redump": RedumpIngestor, "gog": GogIngestor}
# name -> Refresher class (instantiated per run with a Context). Tests register fakes here.
REFRESHERS: dict[str, type] = {"igdb": IgdbRefresher, "steam": SteamRefresher, "gog": GogSizeRefresher,
                                   "wikidata": WikidataRefresher, "steam_pics": SteamPicsRefresher, "rawg": RawgRefresher}

# Not written yet, with the reason, so the UI can be honest about it.
PLANNED = {
    "pcgamingwiki": "Finer DRM detail (e.g. DRM-free Steam builds, Denuvo). PCGamingWiki changed its API "
                    "in Aug 2026: queries now need a bot-password login and the main table was renamed, "
                    "so it needs its own account setup.",
    "gogdb": "Bulk GOG dumps (~60 MB tar.xz per day at gogdb.org/backups_v3) as a faster way to get every "
             "installer size. The internal file layout is undocumented; needs a sample dump to implement.",
    "consoles-prices": "Price lookup for retro console games (PriceCharting's API is paid).",
}


def refreshers_for(field: str) -> list[type]:
    return [c for c in REFRESHERS.values() if field in c.fields]
