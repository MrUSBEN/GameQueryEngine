"""Runs in its own process: asks Steam's PICS for many apps at once (anonymous login) and prints one
JSON line per app. Needs the optional add-on:  pip install "steam[client]".

Kept separate from the web server so the client library (which uses gevent) cannot interfere with it and
a crash or hang here cannot take the app down. Reads {"appids": [...], "batch": 200} on stdin.

Note: PICS has no "list every app id" call. `get_changes_since` only updates an EXISTING watchlist you
already have a baseline for; a cold "since=1" request just tells you a full resync is needed, with no
list attached (confirmed by testing against the live service). Getting the list of every Steam app id
therefore goes through the official Web API (IStoreService/GetAppList, needs a free key) - see
gqe/service.py:_steam_store_list - not through this worker.
"""
from __future__ import annotations

import json
import sys

from .depots import NON_GAME_TYPES, app_summary, app_type, compute_install_size


def run(appids, emit, client_factory, batch: int = 200, timeout: int = 30,
       progress_offset: int = 0, progress_total: int | None = None) -> int:
    """`progress_offset`/`progress_total` let the caller report progress against a GRAND total when this
    process only handles one slice of a much bigger job (see steam_pics.RESTART_EVERY)."""
    client = client_factory()
    result = client.anonymous_login()
    if int(result) != 1:
        emit({"error": f"Steam anonymous login failed ({result})"})
        return 1
    total_for_display = progress_total if progress_total is not None else len(appids)
    done = 0
    for i in range(0, len(appids), batch):
        chunk = appids[i:i + batch]
        info = None
        for _attempt in range(2):
            try:
                info = client.get_product_info(apps=chunk, timeout=timeout)
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as e:
                # gevent.Timeout (raised after a long-lived connection goes stale) is a BaseException,
                # NOT an Exception - deliberately, so naive `except Exception` blocks don't swallow it.
                # Catching only Exception here is exactly what let an hour-long run crash the whole job.
                emit({"warning": f"{e.__class__.__name__}: {e}"})
                info = None
                continue
            if info:
                break
        if info is None:
            # both attempts failed (a request-level problem, not "Steam confirms this app has no data"):
            # report distinctly so the caller retries these later instead of writing them off for good.
            for aid in chunk:
                emit({"appid": aid, "status": "error"})
        else:
            apps = info.get("apps") or {}
            for aid in chunk:
                app = apps.get(aid) or apps.get(str(aid))
                if not app:
                    emit({"appid": aid, "status": "unknown"})
                elif app.get("_missing_token"):
                    emit({"appid": aid, "status": "needs_token"})
                elif app_type(app) in NON_GAME_TYPES:             # soundtrack, DLC, tool...: not an install
                    emit({"appid": aid, "status": "not_a_game", "type": app_type(app), "size": None, "summary": app_summary(app)})
                else:
                    res = compute_install_size(app)
                    emit({"appid": aid, "status": "ok" if res else "no_depots", **(res or {"size": None}),
                          "summary": app_summary(app)})
        done += len(chunk)
        emit({"progress": progress_offset + done, "total": total_for_display})
    try:
        client.logout()
    except Exception:
        pass
    return 0


def main() -> int:
    spec = json.load(sys.stdin)
    try:
        from steam.client import SteamClient
    except ImportError:
        print(json.dumps({"error": "The 'steam' add-on is not installed"}), flush=True)
        return 2
    emit = lambda m: print(json.dumps(m), flush=True)  # noqa: E731
    return run(spec["appids"], emit, SteamClient, spec.get("batch", 200),
              progress_offset=spec.get("progress_offset", 0), progress_total=spec.get("progress_total"))


if __name__ == "__main__":
    sys.exit(main())
