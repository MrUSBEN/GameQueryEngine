"""Polite HTTP: per-host rate limits, retry with backoff (honours Retry-After), and an
on-disk cache so repeating a refresh doesn't hammer anyone. Standard library only.
`transport`, `sleep` and `clock` are injectable so tests never touch the network."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urlparse

from . import APP_ID, __version__, cancel
from .logs import safe_url

USER_AGENT = f"{APP_ID}/{__version__} (personal local tool)"
log = logging.getLogger("gqe.http")

# Minimum seconds between requests, per host. Derived from published/observed limits:
#   IGDB 4 req/s; Steam store ~200 req/5 min; GOG products API ~200 req/hour/IP.
DEFAULT_INTERVALS = {
    "api.igdb.com": 0.28,
    "id.twitch.tv": 0.5,
    "store.steampowered.com": 1.6,
    "catalog.gog.com": 0.4,
    "api.gog.com": 18.5,
    "query.wikidata.org": 2.0,
    "steamspy.com": 61.0,          # SteamSpy allows one "all" page per minute
    "api.rawg.io": 0.35,
}


MAX_WAIT = 120.0   # never sleep longer than this because a server told us to; fail with a clear message instead


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {urlparse(url).netloc}: {body[:200]}")
        self.status, self.body, self.url = status, body, url
        self.retry_after: float | None = None


class RateLimiter:
    def __init__(self, min_interval: float, clock=time.monotonic, sleep=time.sleep):
        self.min_interval, self._clock, self._sleep = min_interval, clock, sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> float:
        """Blocks until allowed; returns how long it had to wait (seconds)."""
        delay = 0.0
        with self._lock:
            if self._last is not None:
                delay = self._last + self.min_interval - self._clock()
                if delay > 0:
                    self._sleep(delay)
            self._last = self._clock()
        return max(delay, 0.0)


def _urllib_transport(method, url, headers, body):
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


class Http:
    def __init__(self, cache_dir=None, transport=None, intervals=None,
                 sleep=None, clock=time.monotonic):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._transport = transport or _urllib_transport
        self._sleep, self._clock = sleep or cancel.sleep, clock
        self._intervals = {**DEFAULT_INTERVALS, **(intervals or {})}
        self._limiters: dict[str, RateLimiter] = {}
        self._lock = threading.Lock()

    def _limiter(self, host: str) -> RateLimiter:
        with self._lock:
            if host not in self._limiters:
                self._limiters[host] = RateLimiter(self._intervals.get(host, 0.2), self._clock, self._sleep)
            return self._limiters[host]

    # -- cache
    def _cache_file(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _cache_get(self, key: str, ttl: float):
        try:
            d = json.loads(self._cache_file(key).read_text("utf-8"))
            if time.time() - d["t"] <= ttl:
                return d["text"]
        except (OSError, ValueError, KeyError):
            pass
        return None

    def _cache_put(self, key: str, text: str) -> None:
        try:
            f = self._cache_file(key)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"t": time.time(), "text": text}), "utf-8")
        except OSError:
            pass

    # -- requests
    def request(self, method: str, url: str, params=None, headers=None, body=None,
                ttl: float = 0, retries: int = 3) -> str:
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        data = body.encode() if isinstance(body, str) else body
        key = hashlib.sha256(f"{method} {url} ".encode() + (data or b"")).hexdigest()
        if ttl and self.cache_dir:
            hit = self._cache_get(key, ttl)
            if hit is not None:
                log.debug("%s %s -> cached", method, safe_url(url))
                return hit
        host = urlparse(url).netloc
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        attempt = 0
        shown = safe_url(url)
        while True:
            waited = self._limiter(host).wait()
            if waited >= 1:
                log.info("Waiting %.0fs (rate limit for %s)", waited, host)
            t0 = time.monotonic()
            try:
                status, rh, raw = self._transport(method, url, hdrs, data)
            except OSError as e:          # URLError/timeouts
                status, rh, raw = 0, {}, str(e).encode()
            took = time.monotonic() - t0
            if status in (0, 429, 500, 502, 503, 504) and attempt < retries:
                attempt += 1
                ra = rh.get("retry-after", "")
                delay = float(ra) if ra.replace(".", "", 1).isdigit() else min(2 ** attempt * 2, 60)
                if delay > MAX_WAIT:
                    log.warning("%s %s -> %s: the server asks us to wait %.0fs; not waiting that long", method, shown, status, delay)
                    err = HttpError(status, f"The server asked us to wait {delay:.0f} seconds (about {delay / 60:.0f} minutes) "
                                            "and we won't wait that long. Try again later.", url)
                    err.retry_after = delay
                    raise err
                log.warning("%s %s -> %s; retry %d/%d in %.0fs", method, shown, status or "no response", attempt, retries, delay)
                self._sleep(delay)
                continue
            break
        text = raw.decode("utf-8", "replace")
        if not 200 <= status < 300:
            log.warning("%s %s -> HTTP %s: %s", method, shown, status, text[:120].replace("\n", " "))
            raise HttpError(status, text, url)
        log.info("%s %s -> %s (%.2fs, %.0f KB)", method, shown, status, took, len(raw) / 1024)
        if ttl and self.cache_dir:
            self._cache_put(key, text)
        return text

    def get_json(self, url, params=None, headers=None, ttl=0):
        return json.loads(self.request("GET", url, params, headers, None, ttl))

    def post_json(self, url, body="", params=None, headers=None, ttl=0):
        return json.loads(self.request("POST", url, params, headers, body, ttl))
