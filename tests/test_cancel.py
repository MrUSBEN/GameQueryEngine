import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import claims, db, logs, query, service
from gqe.adapters import steam_pics
from gqe.cancel import Cancelled
from gqe.ingest import upsert_release
from gqe.server import Api, Jobs, make_handler
from tests.test_sources import Fake, ctx_for
from tests.test_steam_sizes import APP, runner_for


def wait_state(jobs, jid, want, seconds=5):
    for _ in range(int(seconds * 100)):
        if jobs.get(jid)["state"] == want:
            return jobs.get(jid)
        time.sleep(0.01)
    raise AssertionError(f"job stayed {jobs.get(jid)['state']!r}, wanted {want!r}")


class JobCancel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logs.setup(console=False, file=False)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "t.db"
        c = db.connect(self.db)
        upsert_release(c, source="gog", source_key="1", name="Keep Me", platform="pc")
        c.commit()
        c.close()
        self.jobs = Jobs(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cancel_stops_at_next_progress_and_keeps_uncommitted_work(self):
        started = threading.Event()

        def work(conn, progress):
            for i in range(1, 10_000):
                conn.execute("INSERT INTO games(name,name_norm) VALUES(?,?)", (f"g{i}", f"g{i}"))    # NOT committed here
                started.set()
                time.sleep(0.005)
                progress(f"step {i}", None)                                                           # the safe stopping point
            return {"finished": True}

        jid = self.jobs.start("Long job", work)
        self.assertTrue(started.wait(2))
        self.jobs.cancel(jid)
        self.assertTrue(self.jobs.get(jid)["cancelling"])
        j = wait_state(self.jobs, jid, "cancelled")
        self.assertIn("Everything saved before you cancelled was kept", j["message"])
        self.assertFalse(j["cancelling"])
        self.assertIsNone(j["result"])                                                                # it did not run to the end
        conn = db.connect(self.db)
        n = conn.execute("SELECT COUNT(*) FROM games WHERE name LIKE 'g%'").fetchone()[0]
        self.assertGreater(n, 0)                                                                      # partial work was committed
        self.assertLess(n, 9_999)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM games WHERE name='Keep Me'").fetchone()[0], 1)

    def test_cannot_cancel_a_finished_or_unknown_job(self):
        jid = self.jobs.start("Quick", lambda c, p: {"ok": 1})
        wait_state(self.jobs, jid, "done")
        with self.assertRaises(Api):
            self.jobs.cancel(jid)
        with self.assertRaises(Api):
            self.jobs.cancel(9999)

    def test_cancelled_refresh_keeps_the_games_already_done(self):
        conn = db.connect(self.db)
        ids = [upsert_release(conn, source="gog", source_key=f"k{i}", name=f"Game {i}", platform="pc") for i in range(20)]
        for rid in ids:
            conn.execute("INSERT INTO source_link(source,source_key,game_id,release_id) SELECT 'steam',?,game_id,id FROM releases WHERE id=?", (str(1000 + rid), rid))
        conn.commit()
        conn.close()
        release = threading.Event()

        def runner(appids, progress):
            for n, a in enumerate(appids, 1):
                yield {"appid": a, "status": "ok", "size": 1_000_000_000 + a, "kind": "manifest", "depots": 1, "original_year": None}
                if n == 5:
                    release.set()
                    time.sleep(0.3)                                                                   # give the test time to click Cancel
                if n % 2 == 0:
                    progress(f"Steam depot sizes: {n}/{len(appids)} games", n / len(appids))

        def work(c, progress):
            ctx = ctx_for(c, Fake([]), {})
            ctx.services["pics_runner"] = runner
            return service.refresh_fields(c, ctx, ["size"], query.ids(c, ["platform=pc"])[1:], "steam_pics", progress)

        jid = self.jobs.start("Steam depot sizes", work)
        self.assertTrue(release.wait(3))
        self.jobs.cancel(jid)
        wait_state(self.jobs, jid, "cancelled")
        conn = db.connect(self.db)
        sized = query.count(conn, ["size_conf=depot"])
        self.assertGreaterEqual(sized, 4)                                                             # everything before the stop was saved
        self.assertLess(sized, 20)                                                                    # and it really stopped early


class WorkerKilled(unittest.TestCase):
    """The real subprocess, with a deliberately slow stand-in Steam library."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        pkg = self.tmp / "steam"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "client.py").write_text(
            "import time\n"
            "APP = " + repr(APP) + "\n"
            "class SteamClient:\n"
            "    calls = 0\n"
            "    def anonymous_login(self): return 1\n"
            "    def get_product_info(self, apps=None, timeout=15):\n"
            "        SteamClient.calls += 1\n"
            "        time.sleep(0.2 if SteamClient.calls == 1 else 30)   # after batch 1 it would run for a long time\n"
            "        return {'apps': {a: APP for a in apps}}\n"
            "    def logout(self): pass\n")
        self.saved = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(self.tmp)

    def tearDown(self):
        steam_pics.kill_workers()
        os.environ.pop("PYTHONPATH", None) if self.saved is None else os.environ.__setitem__("PYTHONPATH", self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cancelling_kills_the_worker_process(self):
        seen = {}

        def progress(msg, frac=None):
            seen["proc"] = seen.get("proc") or list(steam_pics._WORKERS)[0]
            raise Cancelled()                                                # the user pressed Cancel

        t0 = time.monotonic()
        with self.assertRaises(Cancelled):
            list(steam_pics._subprocess_runner(list(range(1, 601)), progress))       # 3 batches x 0.4 s if it ran to the end
        self.assertLess(time.monotonic() - t0, 3)                                    # stopped after the first batch
        proc = seen["proc"]
        for _ in range(100):                                                         # allow 2 s; unkilled, it would sleep 30 s
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(proc.poll(), "worker process is still running after cancel")
        self.assertEqual(len(steam_pics._WORKERS), 0)

    def test_kill_workers_stops_stragglers(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        steam_pics._WORKERS.add(p)
        steam_pics.kill_workers()
        self.assertIsNotNone(p.poll())
        self.assertEqual(len(steam_pics._WORKERS), 0)


class SkipCheck(unittest.TestCase):
    def test_reruns_only_ask_about_games_without_a_steam_size(self):
        conn = db.connect(":memory:")
        ids = {}
        for k, appid in (("done", 11), ("todo", 22), ("estimate", 33), ("exact", 44)):
            ids[k] = upsert_release(conn, source="gog", source_key=k, name=f"Game {k}", platform="pc")
            conn.execute("INSERT INTO source_link(source,source_key,game_id,release_id) SELECT 'steam',?,game_id,id FROM releases WHERE id=?", (str(appid), ids[k]))
        claims.record_claim(conn, ids["done"], "size", "steam_pics", 2_000_000_000, "depot")          # already got a Steam size
        claims.record_claim(conn, ids["estimate"], "size", "steam_sysreq", 1_000_000_000, "sysreq_estimate")
        claims.record_claim(conn, ids["exact"], "size", "gog", 3_000_000_000, "exact")                # GOG exact: never asked
        conn.commit()
        asked = []

        def ctx_with(results):
            c = ctx_for(conn, Fake([]), {})
            c.services["pics_runner"] = runner_for(results, asked)
            return c

        ok = lambda a, size=5_000_000_000: {"appid": a, "status": "ok", "size": size, "kind": "manifest", "depots": 1, "original_year": None}   # noqa: E731
        out = service.update_source(conn, ctx_with([ok(22)]), "steam_sizes", {})                     # 33 returns nothing this time
        self.assertEqual(sorted(asked), [22, 33])                                                     # not 11 (has one), not 44 (exact)
        self.assertEqual((out["updated"], out["skipped_already_sized"]), (1, 1))
        asked.clear()
        out = service.update_source(conn, ctx_with([ok(33, 1_500_000_000)]), "steam_sizes", {})
        self.assertEqual(asked, [33])                                                                 # only the one still without a size
        self.assertEqual(out["skipped_already_sized"], 2)
        asked.clear()
        out = service.update_source(conn, ctx_with([]), "steam_sizes", {})
        self.assertEqual(asked, [])                                                                   # nothing left: nothing is asked
        self.assertIn("Nothing to do", out["notes"][0])
        self.assertEqual(out["updated"], 0)
        sizes = {r["name"]: (r["size_bytes"], r["size_source"]) for r in query.select(conn)}
        self.assertEqual(sizes["Game exact"], (3_000_000_000, "gog"))                                 # never overwritten
        self.assertEqual(sizes["Game todo"], (5_000_000_000, "steam_pics"))


class CancelApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        logs.setup(console=False, file=False)
        cls.home = Path(tempfile.mkdtemp())
        os.environ["GQE_HOME"] = str(cls.home)
        cls.path = cls.home / "t.db"
        db.connect(cls.path).close()
        cls.jobs = Jobs(cls.path)
        ref = {}
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.path, cls.jobs, ref, 0))
        ref["server"] = cls.srv
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.environ.pop("GQE_HOME", None)
        shutil.rmtree(cls.home, ignore_errors=True)

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers={"X-GQE": "1"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_cancel_endpoint(self):
        def work(conn, progress):
            for i in range(1000):
                time.sleep(0.01)
                progress(f"tick {i}", None)
        jid = self.jobs.start("Endpoint job", work)
        listing = json.loads(urllib.request.urlopen(urllib.request.Request(self.base + "/api/jobs")).read())["jobs"]
        self.assertFalse(next(j for j in listing if j["id"] == jid)["cancelling"])
        self.assertEqual(self.post("/api/job_cancel", {"id": jid}), (200, {"ok": True}))
        wait_state(self.jobs, jid, "cancelled")
        code, out = self.post("/api/job_cancel", {"id": jid})                                        # already stopped
        self.assertEqual(code, 400)
        self.assertIn("isn't running", out["error"])


if __name__ == "__main__":
    unittest.main()
