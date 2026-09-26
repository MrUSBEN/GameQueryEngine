"""Wikidata: free (no key) original release dates for console games.

Data is CC0. Uses the public SPARQL endpoint. Properties used (confirmed on Wikidata):
P31 instance of, P279 subclass of, Q7889 video game, P400 platform, P577 publication date.

* Platform items are found at run time by their English label (the label that is used as
  `platform` by the most games), so no hard-coded Q-ids can be wrong. The labels themselves are
  still hand-picked strings and were checked against live Wikidata pages while building this;
  "Nintendo GameCube" (not "GameCube") is the one that was wrong in an earlier version and wasted
  a query every run without ever finding a match.
* A platform lookup's result - found, or "not enough games under this label" - is cached (the
  positive result forever, the negative one for a week), so a platform that genuinely isn't on
  Wikidata under this app's guess doesn't get re-queried on every single run.
* Wikidata's query service throttles by client and bans repeat offenders for longer if they keep
  querying through a 429 (its own runbook says so). So on a 429, EVERY further Wikidata request -
  across every platform in this run, and any future run - is refused instantly with a clear,
  accurate wait time until the cooldown the server asked for has passed, rather than trying again
  and risking a longer, harsher block.
* Dates keep their stated precision: a year-only date stays "2002", never "2002-01-01".
* Release date = earliest date stated FOR THIS PLATFORM (falls back to earliest overall);
  original year = earliest date on any platform.
* Matching is by normalised English label; unmatched games are left blank, never guessed.
* Coverage is partial (~86% of Wikidata's games have a date) and PC is skipped: the PC
  catalogue is too large for one query. GOG (original dates) and IGDB cover PC.
Not implemented: an ID bridge to GOG. Wikidata's GOG property (P2725) has a separate
"GOG product ID" qualifier, so its main values are probably slugs, which is unverified.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..http import HttpError
from ..ingest import norm_name
from .base import Refresher, Update

ENDPOINT = "https://query.wikidata.org/sparql"
LABELS = {"ps1": "PlayStation", "ps2": "PlayStation 2", "ps3": "PlayStation 3",
          "psp": "PlayStation Portable", "gc": "Nintendo GameCube", "wii": "Wii", "xbox": "Xbox",
          "x360": "Xbox 360", "dc": "Dreamcast", "saturn": "Sega Saturn"}
DAY = 86400
NEGATIVE_TTL_DAYS = 7            # re-check a platform we didn't find, in case Wikidata's data grew
DEFAULT_COOLDOWN = 300.0         # used if a 429 has no usable Retry-After
COOLDOWN_KEY = "cooldown_until"
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def fmt_date(value: str, precision: int) -> str | None:
    m = _DATE.match(value or "")
    if not m or precision < 9:
        return None
    y, mo, d = m.groups()
    return y if precision == 9 else f"{y}-{mo}" if precision == 10 else f"{y}-{mo}-{d}"


def _now():
    return datetime.now(timezone.utc)


def _cooldown_remaining(ctx) -> float:
    until = ctx.state_get("wikidata", COOLDOWN_KEY)
    if not until:
        return 0.0
    try:
        return max((datetime.fromisoformat(until) - _now()).total_seconds(), 0.0)
    except ValueError:
        return 0.0


def _set_cooldown(ctx, seconds: float) -> None:
    ctx.state_set("wikidata", COOLDOWN_KEY, (_now() + timedelta(seconds=max(seconds, 1))).isoformat())


def cooldown_message(remaining: float) -> str:
    mins = max(1, round(remaining / 60))
    return (f"Wikidata asked us to slow down a little while ago and hasn't had time to reset yet "
            f"(about {mins} more minute{'s' if mins != 1 else ''}). Try again after that.")


class WikidataThrottled(LookupError):
    """The source is unavailable right now (a 429): must be surfaced, never silently absorbed."""


class PlatformNotFound(LookupError):
    """Wikidata answered fine, it just has no platform matching this label with enough games: benign,
    skip to the next platform. Deliberately its own type - NOT a bare LookupError - because Python's
    KeyError/IndexError are themselves LookupError subclasses, and a genuinely malformed response
    (a real bug) must not be mistaken for this ordinary, expected case."""


class WikidataRefresher(Refresher):
    name = "wikidata"
    fields = frozenset({"orig_year", "release_date"})
    manual_only = fields          # Wikimedia throttles hard: only when chosen explicitly, never under "Automatic"

    def supports(self, row):
        return row["platform"] in LABELS

    def estimate(self, rows, fields, links):
        return len({r["platform"] for r in rows}) * 15.0

    def _sparql(self, query: str, ttl: float):
        remaining = _cooldown_remaining(self.ctx)
        if remaining > 0:
            raise WikidataThrottled(cooldown_message(remaining))
        try:
            return self.ctx.http.get_json(ENDPOINT, params={"query": query, "format": "json"},
                                          headers={"Accept": "application/sparql-results+json"},
                                          ttl=ttl)["results"]["bindings"]
        except HttpError as e:
            if e.status == 429:
                _set_cooldown(self.ctx, getattr(e, "retry_after", None) or DEFAULT_COOLDOWN)
                raise WikidataThrottled(cooldown_message(_cooldown_remaining(self.ctx))) from None
            raise

    def _platform_qid(self, plat: str) -> str:
        label = LABELS[plat]
        cached = self.ctx.state_get("wikidata", f"platform:{plat}")
        if isinstance(cached, dict):
            if cached.get("qid"):
                return cached["qid"]
            until = cached.get("unavailable_until")
            if until:
                try:
                    if datetime.fromisoformat(until) > _now():
                        raise PlatformNotFound(f"Wikidata has no platform called '{label}' with enough games "
                                               f"(checked recently); skipping {plat}.")
                except ValueError:
                    pass
        rows = self._sparql(
            f'SELECT ?p (COUNT(?g) AS ?n) WHERE {{ ?p rdfs:label "{label}"@en . ?g wdt:P400 ?p . }} '
            f"GROUP BY ?p ORDER BY DESC(?n) LIMIT 1", 30 * DAY)
        if not rows or int(rows[0]["n"]["value"]) < 50:
            until = (_now() + timedelta(days=NEGATIVE_TTL_DAYS)).isoformat()
            self.ctx.state_set("wikidata", f"platform:{plat}", {"unavailable_until": until})
            raise PlatformNotFound(f"Wikidata has no platform called '{label}' with enough games; skipping {plat}.")
        qid = _qid(rows[0]["p"]["value"])
        self.ctx.state_set("wikidata", f"platform:{plat}", {"qid": qid})
        return qid

    def _bulk(self, qid: str) -> dict:
        rows = self._sparql(
            "SELECT ?item ?label ?date ?prec ?qp WHERE { "
            f"?item wdt:P400 wd:{qid} ; wdt:P31/wdt:P279* wd:Q7889 ; rdfs:label ?label . "
            'FILTER(LANG(?label) = "en") '
            "OPTIONAL { ?item p:P577 ?st . ?st psv:P577 ?v . "
            "?v wikibase:timeValue ?date ; wikibase:timePrecision ?prec . "
            "OPTIONAL { ?st pq:P400 ?qp } } }", 7 * DAY)
        items: dict[str, dict] = {}
        for b in rows:
            it = items.setdefault(_qid(b["item"]["value"]), {"label": b["label"]["value"], "dates": []})
            if "date" in b and "prec" in b:
                text = fmt_date(b["date"]["value"], int(b["prec"]["value"]))
                if text:
                    it["dates"].append((text, _qid(b["qp"]["value"]) if "qp" in b else None))
        return items

    def fetch(self, rows, fields, links):
        remaining = _cooldown_remaining(self.ctx)
        if remaining > 0:
            raise WikidataThrottled(cooldown_message(remaining))
        for plat in sorted({r["platform"] for r in rows}):
            try:
                qid = self._platform_qid(plat)
            except WikidataThrottled:
                raise                                  # a real outage/throttle: the caller needs to see this, not a silent 0
            except PlatformNotFound as e:
                self.ctx.progress(str(e), None)         # just "no match for this platform": benign, try the next one
                continue
            bulk = self._bulk(qid)                      # a throttle here also propagates naturally (WikidataThrottled)
            idx: dict[str, list[dict]] = {}
            for it in bulk.values():
                idx.setdefault(norm_name(it["label"]), []).append(it)
            matched = 0
            for r in (x for x in rows if x["platform"] == plat):
                cands = [c for c in idx.get(norm_name(r["name"]), []) if c["dates"]]
                if not cands:
                    continue
                matched += 1
                dates = min(cands, key=lambda c: min(d for d, _ in c["dates"]))["dates"]
                here = [d for d, q in dates if q == qid]
                if "release_date" in fields:
                    yield Update(r["id"], "release_date", min(here or [d for d, _ in dates]))
                if "orig_year" in fields:
                    yield Update(r["id"], "orig_year", int(min(d for d, _ in dates)[:4]))
            self.ctx.progress(f"Wikidata {plat}: matched {matched} of {sum(1 for x in rows if x['platform'] == plat)}", None)
