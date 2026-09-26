"""Tiny local web server (standard library only). Binds to 127.0.0.1 only."""
from __future__ import annotations

import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import traceback
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import logs, APP_ID, APP_NAME, __version__, adapters, backup, config as cfgmod, db, export, paths, pack as packer, query, service
from .adapters import Context
from . import cancel as cancellation
from .cancel import Cancelled
from .adapters.redump import RedumpIngestor
from .http import Http
from .claims import FIELD_MAP, record_claim
from .filters import Clause, FilterError, canonical_field
from .ingest import import_csv_text

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("gqe.job")
MAX_UPLOAD = 500 * 1024 * 1024
_OPS = {"=", "!=", "<", "<=", ">", ">=", "~", "!~", ":"}


class Jobs:
    """Background work (imports, refreshes) with pollable progress."""

    def __init__(self, db_path, http=None):
        self.db_path, self._jobs, self._lock, self._n = db_path, {}, threading.Lock(), 0
        self._cancel: dict[int, threading.Event] = {}
        self.http = http or Http(cache_dir=paths.cache_dir())

    def ctx(self, conn, progress=None):
        c = Context(conn, self.http, cfgmod.load())
        if progress:
            c.progress = progress
        return c

    def start(self, title, fn, meta: dict | None = None) -> int:
        """meta (optional): {"source": "steam_catalog", "kind": "source", "platforms": [...]}.
        When given, the outcome is recorded to activity_log for the Status tab, whether the job
        finishes, fails or is cancelled - that's what lets Status show 'last checked' even for
        runs that found nothing new."""
        with self._lock:
            self._n += 1
            jid = self._n
            self._jobs[jid] = {"id": jid, "title": title, "state": "running", "cancelling": False,
                               "message": "Starting...", "progress": None, "result": None}
            self._cancel[jid] = threading.Event()
        job, cancel = self._jobs[jid], self._cancel[jid]

        def progress(msg, frac=None):
            if cancel.is_set():
                raise Cancelled()
            if msg != job["message"]:
                log.info("[%s] %s", title, msg)
            job["message"], job["progress"] = msg, frac

        def run():
            cancellation.bind(cancel)                      # waits inside this job can now be cancelled
            conn = db.connect(self.db_path, init=False)
            log.info("Job %d started: %s", jid, title)
            t0 = time.monotonic()
            try:
                job["result"] = fn(conn, progress)
                job["state"], job["message"], job["progress"] = "done", "Finished", 1.0
                log.info("Job %d finished in %.1fs: %s", jid, time.monotonic() - t0, str(job["result"])[:300])
            except Cancelled:
                job["state"], job["cancelling"] = "cancelled", False
                job["message"] = "Cancelled. Everything saved before you cancelled was kept."
                log.warning("Job %d cancelled by user after %.1fs; work done so far was kept", jid, time.monotonic() - t0)
            except Exception as e:  # surfaced to the UI
                msg = str(e) or e.__class__.__name__
                log.error("Job %d failed: %s", jid, msg, exc_info=not isinstance(e, (LookupError, ValueError)))
                job["state"], job["message"] = "error", msg   # log the line BEFORE the state flips, so watchers never race it
            finally:
                # Whatever the outcome - done, cancelled, or crashed - keep whatever work was already
                # written rather than losing it when the connection closes with an open transaction.
                try:
                    conn.commit()
                    if meta and meta.get("source"):
                        service.log_activity(conn, meta.get("kind", "source"), meta["source"],
                                             job["state"] == "done", job.get("message"),
                                             job.get("result"), meta.get("platforms"))
                except Exception:
                    pass
                conn.close()
                cancellation.unbind()

        threading.Thread(target=run, daemon=True).start()
        return jid

    def cancel(self, jid: int) -> None:
        job = self._jobs.get(jid)
        if not job or job["state"] != "running":
            raise Api("That job isn't running.")
        job["cancelling"] = True
        job["message"] = "Cancelling... stopping at the next safe point"
        self._cancel[jid].set()
        log.warning("Cancel requested for job %d (%s)", jid, job["title"])

    def summary(self) -> list:
        with self._lock:
            jobs = list(self._jobs.values())[-8:]
        return [{k: j[k] for k in ("id", "title", "state", "message", "progress", "cancelling")} for j in reversed(jobs)]

    def get(self, jid):
        return self._jobs.get(jid)

    def any_running(self) -> bool:
        return any(j["state"] == "running" for j in self._jobs.values())


class Api(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def clauses_from(body: dict) -> list[Clause]:
    out = []
    for f in body.get("filters") or []:
        op = f.get("op", "=")
        if op not in _OPS:
            raise FilterError(f"bad operator {op!r}")
        val = f.get("value")
        val = ",".join(map(str, val)) if isinstance(val, list) else str(val)
        out.append(Clause(canonical_field(f["field"]), op, val, bool(f.get("or_unknown"))))
    ids = body.get("ids")
    if ids:
        out.append(Clause("id", "=", ",".join(str(int(i)) for i in ids)))
    return out


def make_handler(db_path, jobs: Jobs, server_ref: dict, token_port: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"Game Query Engine/{__version__}"

        def log_message(self, fmt, *args):  # quiet
            pass

        # ---- plumbing
        def _send(self, status, payload, ctype="application/json", headers=None):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _body(self, raw=False):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                raise Api("File too large (limit 500 MB)", 413)
            data = self.rfile.read(n)
            return data if raw else (json.loads(data or b"{}"))

        def _dispatch(self, method):
            if not self._host_ok():
                return self._send(403, {"error": "forbidden"})
            url = urlparse(self.path)
            try:
                if method == "GET" and url.path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if not url.path.startswith("/api/"):
                    return self._send(404, {"error": "not found"})
                if method == "POST" and self.headers.get("X-GQE") != "1":
                    return self._send(403, {"error": "missing header"})
                conn = db.connect(db_path, init=False)
                try:
                    handler = getattr(self, "api_" + url.path[5:].replace("/", "_"), None)
                    if handler is None:
                        return self._send(404, {"error": "unknown endpoint"})
                    out = handler(conn, parse_qs(url.query))
                    if out is not None:
                        self._send(200, out)
                finally:
                    conn.close()
            except (FilterError, ValueError, LookupError, KeyError, Api) as e:
                log.info("Request %s refused: %s", url.path, str(e).strip("'\"")[:200])
                self._send(getattr(e, "status", 400), {"error": str(e).strip("'\"")})
            except Exception as e:
                log.error("Request %s crashed", url.path, exc_info=True)
                self._send(500, {"error": f"{e.__class__.__name__}: {e}"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        # ---- endpoints
        def api_facets(self, conn, q):
            return service.facets(conn)

        def api_stats(self, conn, q):
            return {"platforms": query.stats(conn), "total": query.count(conn)}

        def api_log(self, conn, q):
            return logs.tail(int((q.get("since") or ["0"])[0]))

        def api_log_download(self, conn, q):
            self._send(200, logs.full_text().encode("utf-8"), "text/plain; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="{APP_ID}-log-{time.strftime("%Y%m%d-%H%M")}.txt"'})

        def api_job_cancel(self, conn, q):
            jobs.cancel(int(self._body()["id"]))
            return {"ok": True}

        def api_gaps(self, conn, q):
            return {"platforms": service.gaps_report(conn)}

        def api_status(self, conn, q):
            return service.status_report(conn, Path(db_path))

        def api_jobs(self, conn, q):
            return {"jobs": jobs.summary()}

        def api_info(self, conn, q):
            return {"name": APP_NAME, "version": __version__}

        def api_config(self, conn, q):
            if self.command == "POST":
                cfgmod.update(self._body())
            return cfgmod.public_view(cfgmod.load())

        def api_adapters(self, conn, q):
            cfg = cfgmod.load()
            refreshers = []
            for c in adapters.REFRESHERS.values():
                missing = [k for k in c.needs if not cfgmod.get(cfg, k)]
                refreshers.append({"name": c.name, "fields": sorted(c.fields), "needs": list(c.needs),
                                   "ready": not missing})
            return {"ingestors": list(adapters.INGESTORS), "refreshers": refreshers,
                    "planned": adapters.PLANNED, "fields": list(FIELD_MAP),
                    "extras": {"steam_pics": {"installed": adapters.steam_pics.addon_installed()}}}

        def api_query(self, conn, q):
            b = self._body()
            cl = clauses_from(b)
            size = max(1, min(int(b.get("page_size", 100)), 500))
            page = max(1, int(b.get("page", 1)))
            total = query.count(conn, cl)
            rows = query.select(conn, cl, b.get("sort"), size, (page - 1) * size)
            return {"rows": rows, "total": total, "page": page, "page_size": size}

        def api_export(self, conn, q):
            b = self._body()
            fmt = b.get("format", "csv")
            rows = query.select(conn, clauses_from(b), b.get("sort"))
            text = export.render(rows, fmt, b.get("detail", "standard"), b.get("columns"))
            data = text.encode("utf-8-sig" if fmt == "csv" else "utf-8")
            stamp = time.strftime("%Y%m%d-%H%M")
            self._send(200, data, export.FORMATS[fmt] + "; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="gqe-{stamp}.{fmt}"',
                        "X-Row-Count": str(len(rows))})

        def api_pack(self, conn, q):
            b = self._body()
            rows = query.select(conn, clauses_from(b), "name")
            bins = packer.parse_bins(str(b.get("bins", "")))
            res = packer.pack(rows, bins, rank=b.get("rank", "count"),
                              strategy=b.get("strategy", "optimal"),
                              max_items=int(b["max_items"]) if b.get("max_items") else None,
                              unknown_score=float(b.get("unknown_score", 0)),
                              allow_duplicates=bool(b.get("allow_duplicates")))
            slim = lambda r: {k: r.get(k) for k in ("id", "name", "platform", "region", "year",
                                                      "size_bytes", "size_conf", "score")}
            return {"bins": [{"capacity": x.capacity, "used": x.used, "free": x.free,
                              "items": [slim(i) for i in x.items]} for x in res["bins"]],
                    "total_items": res["total_items"], "total_bytes": res["total_bytes"],
                    "candidates": res["candidates"],
                    "skipped_unknown_size": res["skipped_unknown_size"],
                    "ids": [i["id"] for x in res["bins"] for i in x.items]}

        def _refresh_args(self, conn, b):
            fields = b.get("fields") or ([b["field"]] if b.get("field") else [])
            bad = [f for f in fields if f not in FIELD_MAP and f != "all"]
            if not fields or bad:
                raise Api(f"unknown column {', '.join(bad) or '(none chosen)'}")
            ids = query.ids(conn, clauses_from(b))
            return fields, ids, (b.get("source") or None), bool(b.get("only_missing"))

        def api_refresh_estimate(self, conn, q):
            fields, ids, src, om = self._refresh_args(conn, self._body())
            try:
                return service.estimate_refresh(conn, jobs.ctx(conn), fields, ids, src, om)
            except LookupError as e:
                raise Api(str(e).strip("'\""))

        def api_refresh(self, conn, q):
            fields, ids, src, om = self._refresh_args(conn, self._body())
            meta = {"kind": "refresh", "source": src or "automatic", "platforms": None}
            return {"job": jobs.start(f"Refresh {', '.join(fields)}", lambda c, p:
                                      service.refresh_fields(c, jobs.ctx(c), fields, ids, src, p, om), meta)}

        def api_update_source(self, conn, q):
            b = self._body()
            src, opts = b.get("source"), b.get("options") or {}
            meta = {"kind": "source", "source": src, "platforms": service.source_platforms(src, opts)}
            return {"job": jobs.start(f"Update {src}", lambda c, p:
                                      service.update_source(c, jobs.ctx(c), src, opts, p), meta)}

        def api_update_library(self, conn, q):
            mode = self._body().get("mode", "new")
            if mode not in ("new", "new_and_fill"):
                raise Api("mode must be 'new' or 'new_and_fill'")
            meta = {"kind": "library", "source": f"update_library:{mode}", "platforms": None}
            return {"job": jobs.start(f"Update database ({mode})", lambda c, p:
                                      service.update_library(c, jobs.ctx(c), mode, p), meta)}

        def api_job(self, conn, q):
            job = jobs.get(int(q["id"][0]))
            if not job:
                raise Api("no such job", 404)
            return job

        def api_upload_dat(self, conn, q):
            name = (q.get("name") or ["upload.dat"])[0]
            platform = (q.get("platform") or [""])[0] or None
            variants = (q.get("variants") or ["0"])[0] == "1"
            data = self._body(raw=True)
            tmpdir = tempfile.mkdtemp(prefix="gqe-")
            paths = []
            if name.lower().endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for m in z.namelist():
                        if m.lower().endswith((".dat", ".xml")):
                            p = Path(tmpdir) / re.sub(r"[^\w.-]", "_", Path(m).name)
                            p.write_bytes(z.read(m))
                            paths.append(p)
                if not paths:
                    raise Api("That zip has no .dat file inside")
            else:
                p = Path(tmpdir) / "upload.dat"
                p.write_bytes(data)
                paths.append(p)

            def work(c, progress):
                total = []
                try:
                    for p in paths:
                        total.append(service.build_from_records(
                            c, RedumpIngestor(p, platform, variants), progress))
                finally:
                    for p in paths:
                        p.unlink(missing_ok=True)
                return {"imported": sum(t["records"] for t in total)}

            meta = {"kind": "import", "source": "redump", "platforms": service.source_platforms("redump", {"platform": platform})}
            return {"job": jobs.start("Import Redump DAT", work, meta)}

        def api_import_csv(self, conn, q):
            text = self._body(raw=True).decode("utf-8-sig", "replace")
            r = import_csv_text(conn, text, "manual")
            service.log_activity(conn, "csv", "csv", not r.get("errors"),
                                 f"Applied {r.get('applied', 0)} values", r)
            return r

        def api_override(self, conn, q):
            b = self._body()
            record_claim(conn, int(b["id"]), b["field"], "manual", b["value"])
            conn.commit()
            return {"row": dict(conn.execute("SELECT * FROM v_release WHERE id=?", (int(b["id"]),)).fetchone())}

        def api_flag(self, conn, q):
            b = self._body()
            service.set_user_flag(conn, int(b["game_id"]), b["flag"], bool(b["value"]))
            return {"ok": True}

        def api_flag_bulk(self, conn, q):
            b = self._body()
            return service.set_user_flags(conn, clauses_from(b), b["flag"], bool(b["value"]))

        def api_sets_save(self, conn, q):
            b = self._body()
            name = str(b.get("name", "")).strip()
            if not name:
                raise Api("Give the set a name")
            ids = b.get("ids") or query.ids(conn, clauses_from(b))
            return {"saved": service.save_set(conn, name, [int(i) for i in ids])}

        def api_sets_delete(self, conn, q):
            service.delete_set(conn, str(self._body().get("name")))
            return {"ok": True}

        def api_db_info(self, conn, q):
            return backup.db_info(Path(db_path))

        def api_backup_download(self, conn, q):
            data = backup.make_zip(conn)
            stamp = time.strftime("%Y%m%d-%H%M")
            service.log_activity(conn, "backup", "backup", True, f"Downloaded backup ({len(data)} bytes)")
            self._send(200, data, "application/zip",
                       {"Content-Disposition": f'attachment; filename="{APP_ID}-backup-{stamp}.zip"'})

        def api_restore(self, conn, q):
            if jobs.any_running():
                raise Api("A background update is still running. Wait for it to finish, then restore.")
            data = self._body(raw=True)
            conn.close()
            try:
                r = backup.restore(Path(db_path), data)
                conn2 = db.connect(db_path, init=False)
                try:
                    service.log_activity(conn2, "restore", "restore", True, f"Restored {r.get('releases', 0)} releases", r)
                finally:
                    conn2.close()
                return r
            except backup.BackupError as e:
                raise Api(str(e))

        def api_shutdown(self, conn, q):
            threading.Thread(target=server_ref["server"].shutdown, daemon=True).start()
            return {"ok": True}

    return Handler


def serve(db_path=None, port: int = 8765, open_browser: bool = True) -> None:
    notice = paths.prepare()
    logs.setup()
    if notice:
        logging.getLogger("gqe").info(notice)
    db_path = db_path or db.default_db_path()
    db.connect(db_path).close()  # create/upgrade schema once
    jobs, ref = Jobs(db_path), {}
    server = None
    for p in range(port, port + 30):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), make_handler(db_path, jobs, ref, p))
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("Could not find a free port")
    ref["server"] = server
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    logging.getLogger("gqe").info("%s %s running at %s", APP_NAME, __version__, url)
    logging.getLogger("gqe").info("Database: %s", db_path)
    logging.getLogger("gqe").info("Live log: open the Log tab in the app. Ctrl+C here (or Quit in the page) stops it.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        adapters.steam_pics.kill_workers()
