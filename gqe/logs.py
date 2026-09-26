"""One logging system, three destinations: the launcher window (stdout), a rotating file in
data/logs/, and an in-memory ring buffer that the Log tab reads live.

Secrets are masked on the way out (keys, tokens, passwords, Authorization headers), so a log
can be pasted into a bug report safely.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qsl, urlencode, urlparse

from . import APP_ID, paths

RING_SIZE = 5000
_ring: collections.deque = collections.deque(maxlen=RING_SIZE)
_lock = threading.Lock()
_counter = 0
_ready = False

_SECRET_PARAM = re.compile(r"(?i)\b(key|client_secret|access_token|token|password|api_key|authorization)=([^&\s\"']+)")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")
_SENSITIVE = {"key", "client_secret", "access_token", "token", "password", "api_key"}


def redact(text: str) -> str:
    return _BEARER.sub(r"\1 ***", _SECRET_PARAM.sub(r"\1=***", text))


def safe_url(url: str, limit: int = 110) -> str:
    """host/path?query for log lines: secret params masked, long queries (SPARQL) shortened."""
    p = urlparse(url)
    pairs = [(k, "***" if k.lower() in _SENSITIVE else v) for k, v in parse_qsl(p.query, keep_blank_values=True)]
    s = p.netloc + p.path + (("?" + urlencode(pairs, safe="*")) if pairs else "")
    return s if len(s) <= limit else s[:limit - 3] + "..."


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), ()
        return True


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _counter
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
        with _lock:
            _counter += 1
            _ring.append({"id": _counter, "t": time.strftime("%H:%M:%S", time.localtime(record.created)),
                          "level": record.levelname, "msg": msg})


def log_file() -> "paths.Path":
    return paths.app_dir() / "logs" / f"{APP_ID}.log"


def setup(console: bool = True, file: bool = True, debug: bool | None = None) -> None:
    """Idempotent. console=stdout (the launcher window), file=data/logs, always the ring buffer."""
    global _ready
    if _ready:
        return
    debug = bool(os.environ.get(f"{APP_ID.upper()}_DEBUG")) if debug is None else debug
    logger = logging.getLogger("gqe")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    ring = RingHandler()
    ring.addFilter(RedactFilter())
    logger.addHandler(ring)
    if console:
        try:
            sys.stdout.reconfigure(errors="replace")      # never crash a Windows console on a special character
        except (AttributeError, ValueError):
            pass
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(fmt)
        h.addFilter(RedactFilter())
        logger.addHandler(h)
    if file:
        try:
            path = log_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
            fh.addFilter(RedactFilter())
            logger.addHandler(fh)
        except OSError:
            pass
    _ready = True


def tail(since: int = 0, limit: int = 1000) -> dict:
    """Lines newer than `since`. First call (since=0) returns only the most recent 300."""
    with _lock:
        lines = [e for e in _ring if e["id"] > since]
        last = _ring[-1]["id"] if _ring else 0
    if since == 0:
        lines = lines[-300:]
    return {"lines": lines[:limit], "last": last}


def full_text() -> str:
    """Log as text for download: the file if there is one, otherwise the in-memory lines."""
    try:
        return log_file().read_text("utf-8")
    except OSError:
        with _lock:
            return "\n".join(f"{e['t']} {e['level']:<7} {e['msg']}" for e in _ring) + "\n"
