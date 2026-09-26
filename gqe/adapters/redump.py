"""Redump DAT ingestor (PS1/PS2/GameCube/Xbox/Dreamcast/... disc images).

Input: a Redump .dat file (clrmamepro XML) downloaded from redump.org/downloads.
Gives EXACT raw image sizes, region and (when present) serial. It does NOT give
release dates or genres - those come from IGDB/MobyGames/Wikidata later.

Multi-disc games are summed into one release (so packing sees the true total).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from typing import Iterator

from ..genres import norm_genres
from ..platforms import normalize_platform
from .base import Ingestor, Record

REGIONS = {
    "Asia", "Australia", "Austria", "Belgium", "Brazil", "Canada", "China", "Croatia", "Czech",
    "Denmark", "Europe", "Finland", "France", "Germany", "Greece", "Hong Kong", "Hungary",
    "Ireland", "Israel", "Italy", "Japan", "Korea", "Latin America", "Netherlands", "New Zealand",
    "Norway", "Poland", "Portugal", "Russia", "Scandinavia", "Singapore", "South Africa", "Spain",
    "Sweden", "Switzerland", "Taiwan", "UK", "USA", "World",
}
_VARIANT_PREFIX = ("beta", "demo", "proto", "sample", "unl", "promo", "kiosk", "preview",
                   "not for resale")


def parse_name(name: str) -> dict:
    groups = re.findall(r"\(([^)]*)\)", name)
    base = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", name).strip()
    m = re.match(r"^(?P<t>.+?), (?P<a>The|A|An)(?P<rest>( - .*)?)$", base)
    if m:
        base = f"{m['a']} {m['t']}{m['rest']}"
    region = None
    for g in groups:
        toks = [t.strip() for t in g.split(",")]
        if toks and all(t in REGIONS for t in toks):
            region = ", ".join(toks)
            break
    return {
        "base": base,
        "region": region,
        "is_disc": bool(re.search(r"\(Disc \d+\)", name)),
        "variant": any(g.lower().startswith(_VARIANT_PREFIX) for g in groups),
        "key": re.sub(r"\s*\(Disc \d+\)", "", name),
    }


_DATE_KEYS = {"releasedate", "release_date", "date", "year", "releaseyear", "release_year"}


def parse_any_date(text) -> str | None:
    """'2004-03-18' | '2004/3/18' | '20040318' | '2004-03' | '2004' -> ISO, keeping only the precision stated."""
    t = str(text or "").strip()
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t) or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", t)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}" if 1950 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 else None
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", t)
    if m and 1950 <= int(m[1]) <= 2100 and 1 <= int(m[2]) <= 12:
        return f"{int(m[1]):04d}-{int(m[2]):02d}"
    m = re.fullmatch(r"\d{4}", t)
    return t if m and 1950 <= int(t) <= 2100 else None


def parse_dat(path: str | Path):
    """Returns (system name, [game dicts]). Besides name/size/serial, picks up a release date and genre when
    the DAT carries them (<release date=..>, <year>, <releasedate>, <releaseyear>, <date>, <genre>, or the same
    as attributes on <game>). Redump's own DATs have none of these; some other DAT sources do."""
    root = ET.parse(str(path)).getroot()
    header = root.find("header")
    system = (header.findtext("name") if header is not None else None) or ""
    games = []
    for g in list(root.iter("game")) + list(root.iter("machine")):
        roms = g.findall("rom")
        if not roms:
            continue
        dates = [parse_any_date(v) for k, v in g.attrib.items() if k.lower() in _DATE_KEYS]
        genre = g.get("genre")
        for child in g:
            tag = child.tag.lower()
            if tag == "release":
                dates.append(parse_any_date(child.get("date")))
            elif tag in _DATE_KEYS:
                dates.append(parse_any_date(child.text))
            elif tag == "genre" and (child.text or "").strip():
                genre = genre or child.text.strip()
        dates = [d for d in dates if d]
        games.append({"name": g.get("name") or "",
                      "size": sum(int(r.get("size") or 0) for r in roms),
                      "serial": next((r.get("serial") for r in roms if r.get("serial")), None),
                      "date": min(dates) if dates else None,
                      "genres": norm_genres(re.split(r"[,/;]", genre)) if genre else []})
    return system, games


class RedumpIngestor(Ingestor):
    name = "redump"

    def __init__(self, path: str | Path, platform: str | None = None,
                 include_variants: bool = False):
        self.path, self.include_variants = Path(path), include_variants
        self.platform = normalize_platform(platform) if platform else None

    def records(self) -> Iterator[Record]:
        system, games = parse_dat(self.path)
        platform = self.platform or normalize_platform(system)
        if not platform:
            raise ValueError("cannot tell the platform from this DAT; specify it")
        grouped: "OrderedDict[str, dict]" = OrderedDict()
        for game in games:
            name = game["name"]
            info = parse_name(name)
            if info["variant"] and not self.include_variants:
                continue
            g = grouped.setdefault(info["key"], {"info": info, "size": 0, "discs": 0, "serial": game["serial"],
                                                 "date": game["date"], "genres": game["genres"]})
            g["size"] += game["size"]
            g["discs"] += 1
        for key, g in grouped.items():
            yield Record(source="redump", source_key=f"{platform}:{key}", name=g["info"]["base"],
                         platform=platform, region=g["info"]["region"], size_bytes=g["size"] or None,
                         size_conf="exact", discs=g["discs"], serial=g["serial"], release_date=g["date"],
                         claims=[("genres", g["genres"], None)] if g["genres"] else [])
