"""Steam depot sizes via PICS (the method SteamDB uses): measured install sizes, in bulk, no key.

Needs the optional add-on `steam[client]` (ValvePython). The client runs in a separate worker
process (gqe/pics_worker.py). Works only for games already linked to a Steam app id: run
"Match my games to Steam" first.

Not verified from the build environment (no internet): how many apps return full depot data to an
anonymous login. That is exactly what the Data-tab test on 200 games measures before you commit.
Sanity guard: if Steam records an ORIGINAL release year that differs by more than 2 years from the
year in your database, the size is skipped (same title, different game, e.g. a remake).
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from .base import Refresher, Update

log = logging.getLogger("gqe.steam")
INSTALL_HINT = ("The optional Steam add-on isn't installed. Use Data > Steam sizes > Install add-on "
                "(or run: pip install \"steam[client]\").")


def addon_installed() -> bool:
    try:
        return importlib.util.find_spec("steam.client") is not None
    except (ImportError, ValueError):
        return False


_WORKERS: set = set()
_WORKERS_LOCK = threading.Lock()


def kill_workers() -> None:
    """Stop any Steam worker still running (called on cancel, on Quit and at interpreter exit)."""
    with _WORKERS_LOCK:
        procs = list(_WORKERS)
        _WORKERS.clear()
    for p in procs:
        try:
            p.kill()
            p.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


atexit.register(kill_workers)


def _run_worker(spec, progress):
    root = str(Path(__file__).resolve().parents[2])
    env = {**os.environ, "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    log.info("Starting the Steam worker process (%s)", spec.get("mode", "info") if spec.get("mode") else f"{len(spec.get('appids', []))} apps")
    proc = subprocess.Popen([sys.executable, "-m", "gqe.pics_worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, cwd=root, env=env)
    with _WORKERS_LOCK:
        _WORKERS.add(proc)
    try:
        proc.stdin.write(json.dumps(spec))
        proc.stdin.close()
        error, tail = None, []
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                tail = (tail + [line])[-3:]
                continue
            msg = json.loads(line)
            if "error" in msg:
                error = msg["error"]
                log.error("Steam worker: %s", error)
            elif "warning" in msg:
                log.warning("Steam worker: %s", msg["warning"])
            elif "progress" in msg:
                progress(f"Steam depot sizes: {msg['progress']}/{msg['total']} games", msg["progress"] / max(1, msg["total"]))
            elif "appid" in msg or "app_ids" in msg or "listed" in msg:
                yield msg
        code = proc.wait()
        if error or code != 0:
            raise RuntimeError(error or "Steam worker stopped unexpectedly: " + " | ".join(tail))
    finally:                                   # cancelled, failed or finished: never leave the worker running
        if proc.poll() is None:
            log.info("Stopping the Steam worker process")
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        with _WORKERS_LOCK:
            _WORKERS.discard(proc)


# A long-lived Steam session was observed to go stale after roughly an hour of continuous work (a
# gevent.Timeout the client library doesn't recover from on its own), which used to crash the whole
# job. Restarting the worker process - a fresh connection - well before that point keeps one stale
# connection from taking down a run of tens of thousands of games; each restart also gives the report
# a natural checkpoint, and `progress_offset`/`progress_total` keep the displayed progress accurate
# across restarts instead of resetting to 0 each time.
RESTART_EVERY = 2000


def _subprocess_runner(appids, progress):
    """Per-app records (size, status, summary) for the given app ids. Restarts the worker process every
    RESTART_EVERY apps for a fresh connection (see the module note above)."""
    appids = list(appids)
    total = len(appids)
    for i in range(0, total, RESTART_EVERY):
        chunk = appids[i:i + RESTART_EVERY]
        spec = {"appids": chunk, "batch": 200, "progress_offset": i, "progress_total": total}
        for msg in _run_worker(spec, progress):
            if "appid" in msg:
                yield msg


class SteamPicsRefresher(Refresher):
    name = "steam_pics"
    link_source = "steam"            # uses the app ids saved by "Match my games to Steam"
    fields = frozenset({"size"})

    def unavailable(self):
        return None if (self.ctx.services.get("pics_runner") or addon_installed()) else INSTALL_HINT

    def supports(self, row):
        return row["platform"] == "pc"

    def estimate(self, rows, fields, links):
        n = sum(1 for r in rows if r["id"] in links)
        return 15 + n * 0.1          # a guess: unmeasured until the 200-game test has been run

    def run(self, appids, progress=None):
        runner = self.ctx.services.get("pics_runner") or _subprocess_runner
        yield from runner(list(appids), progress or self.ctx.progress)

    def fetch(self, rows, fields, links):
        by_app = {int(links[r["id"]]): r for r in rows if str(links.get(r["id"], "")).isdigit()}
        mismatched = 0
        for res in self.run(list(by_app)):
            row = by_app.get(res["appid"])
            if not row or not res.get("size"):
                continue
            oy, mine = res.get("original_year"), row.get("orig_year") or row.get("year")
            if oy and mine and abs(oy - mine) > 2:
                mismatched += 1                                    # same title, different game
                continue
            yield Update(row["id"], "size", res["size"], "depot")
        if mismatched:
            self.ctx.progress(f"Skipped {mismatched} games whose Steam release year didn't match yours", None)
