import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import config, db, logs, service
from gqe.http import Http
from gqe.server import Jobs, make_handler
from tests.test_sources import Fake
from tests.test_steam_sizes import igdb_links_fake


def since_mark():
    return logs.tail(0)["last"]


def messages(since):
    return [e["msg"] for e in logs.tail(since)["lines"]]


def wait_done(jobs, jid, seconds=5):
    for _ in range(int(seconds * 50)):
        j = jobs.get(jid)
        if j["state"] != "running":
            return j
        time.sleep(0.02)
    raise AssertionError("job did not finish")


class Redaction(unittest.TestCase):
    def test_secrets_are_masked_everywhere(self):
        self.assertEqual(logs.redact("GET x?key=ABC&a=1 client_secret=zzz"), "GET x?key=***&a=1 client_secret=***")
        self.assertEqual(logs.redact("Authorization: Bearer abc.def-1"), "Authorization: Bearer ***")
        u = logs.safe_url("https://api.steampowered.com/IStoreService/GetAppList/v1/?key=SECRET&max_results=50000")
        self.assertNotIn("SECRET", u)
        self.assertIn("key=***", u)
        long = logs.safe_url("https://query.wikidata.org/sparql?query=" + "SELECT+" * 100)
        self.assertLessEqual(len(long), 110)


class SafetyNet(unittest.TestCase):
    """Even if some future code logs a raw secret by mistake, it must not reach any log output."""

    @classmethod
    def setUpClass(cls):
        logs.setup(console=False, file=False)

    def test_a_carelessly_logged_secret_is_still_masked(self):
        import logging
        mark = since_mark()
        logging.getLogger("gqe.oops").info("calling https://x.test/api?key=RAWSECRET&a=1 with Bearer RAWTOKEN99")
        logging.getLogger("gqe.oops").info("password=hunter2 for %s", "user")
        text = "\n".join(messages(mark))
        for secret in ("RAWSECRET", "RAWTOKEN99", "hunter2"):
            self.assertNotIn(secret, text)
        self.assertIn("key=***", text)


class HttpLogging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logs.setup(console=False, file=False)

    def test_requests_retries_waits_and_cache_are_logged_without_secrets(self):
        mark, n = since_mark(), {"c": 0}

        def flaky(m, u, b):
            n["c"] += 1
            return (429, {"retry-after": "7"}, b"slow") if n["c"] == 1 else {"ok": True}

        tmp = tempfile.mkdtemp()
        try:
            http = Http(cache_dir=tmp, transport=Fake([("https://slow.test/", flaky)]), sleep=lambda s: None,
                        intervals={"slow.test": 5.0})
            http.get_json("https://slow.test/api", params={"key": "TOPSECRET", "q": "x"}, ttl=60)
            http.get_json("https://slow.test/api", params={"key": "TOPSECRET", "q": "x"}, ttl=60)     # served from cache
            http.get_json("https://slow.test/other", ttl=0)                                            # hits the rate limiter
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        msgs = messages(mark)
        joined = "\n".join(msgs)
        self.assertIn("retry 1/3 in 7s", joined)                                   # the user sees why it is slow
        self.assertRegex(joined, r"GET slow\.test/api\?key=\*\*\*&q=x -> 200 \(\d+\.\d\ds, \d+ KB\)")
        self.assertIn("Waiting", joined)                                           # rate limit made it wait
        self.assertNotIn("TOPSECRET", joined)
        self.assertEqual(sum("/api" in m and "-> 200" in m for m in msgs), 1)      # the cached call did not hit the network


class JobLogging(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logs.setup(console=False, file=False)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = os.environ.get("GQE_HOME")
        os.environ["GQE_HOME"] = str(self.tmp)
        self.db = self.tmp / "t.db"
        db.connect(self.db).close()
        self.jobs = Jobs(self.db, http=Http(sleep=lambda s: None))

    def tearDown(self):
        os.environ.pop("GQE_HOME", None) if self.saved is None else os.environ.__setitem__("GQE_HOME", self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_progress_result_and_failures_are_logged(self):
        mark = since_mark()

        def work(conn, progress):
            progress("step A", 0.1)
            progress("step A", 0.2)                                              # repeated message: not repeated in the log
            progress("step B", 0.9)
            return {"n": 1}

        jid = self.jobs.start("Demo job", work)
        self.assertEqual(wait_done(self.jobs, jid)["state"], "done")
        msgs = messages(mark)
        self.assertTrue(any(m.startswith(f"Job {jid} started: Demo job") for m in msgs))
        self.assertEqual(sum(m == "[Demo job] step A" for m in msgs), 1)
        self.assertIn("[Demo job] step B", msgs)
        self.assertTrue(any(m.startswith(f"Job {jid} finished") and "{'n': 1}" in m for m in msgs))

        mark = since_mark()
        j1 = self.jobs.start("Expected failure", lambda c, p: (_ for _ in ()).throw(LookupError("no keys yet")))
        wait_done(self.jobs, j1)
        j2 = self.jobs.start("Bug", lambda c, p: 1 / 0)
        wait_done(self.jobs, j2)
        msgs = messages(mark)
        expected = next(m for m in msgs if "Expected failure" not in m and f"Job {j1} failed" in m)
        self.assertIn("no keys yet", expected)
        self.assertNotIn("Traceback", expected)                                  # a clear message, no noise
        bug = next(m for m in msgs if f"Job {j2} failed" in m)
        self.assertIn("Traceback", bug)                                          # a real bug keeps its stack trace
        self.assertIn("ZeroDivisionError", bug)

    def test_steam_match_job_is_traceable_and_never_leaks_the_key(self):
        real = db.connect(self.db)
        from gqe.ingest import upsert_release
        upsert_release(real, source="igdb", source_key="pc:100", name="Half Life", platform="pc")
        real.commit()
        real.close()
        config.update({"igdb": {"client_id": "cid", "client_secret": "sec"}, "steam": {"api_key": "SECRETKEY123"}})
        fake = igdb_links_fake([])
        fake.routes.append(("https://api.steampowered.com/IStoreService/GetAppList/v1/",
                            {"response": {"apps": [], "have_more_results": False}}))
        jobs = Jobs(self.db, http=Http(transport=fake, sleep=lambda s: None))
        mark = since_mark()
        jid = jobs.start("Update steam_match", lambda c, p: service.update_source(c, jobs.ctx(c, p), "steam_match", {}, p))
        j = wait_done(jobs, jid)
        self.assertEqual(j["state"], "done", j["message"])
        joined = "\n".join(messages(mark))
        self.assertIn("IStoreService/GetAppList/v1/?key=***", joined)               # the request is visible...
        self.assertNotIn("SECRETKEY123", joined)                                     # ...the secret is not
        self.assertIn("api.igdb.com/v4/external_games", joined)
        self.assertIn("Steam match done", joined)


class LogApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logs.setup(console=False, file=False)
        from http.server import ThreadingHTTPServer
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

    def get(self, path):
        with urllib.request.urlopen(urllib.request.Request(self.base + path, headers={"X-GQE": "1"})) as r:
            return r.read(), r.headers

    def test_endpoints(self):
        import logging
        logging.getLogger("gqe.test").info("marker-line-123")
        first = json.loads(self.get("/api/log?since=0")[0])
        self.assertTrue(any("marker-line-123" in e["msg"] for e in first["lines"]))
        logging.getLogger("gqe.test").warning("second-marker")
        newer = json.loads(self.get(f"/api/log?since={first['last']}")[0])
        self.assertEqual([e["msg"] for e in newer["lines"]], ["second-marker"])            # only what is new
        self.assertEqual(newer["lines"][0]["level"], "WARNING")
        jid = self.jobs.start("Listed job", lambda c, p: time.sleep(0.3))
        listing = json.loads(self.get("/api/jobs")[0])["jobs"]
        self.assertEqual((listing[0]["title"], listing[0]["state"]), ("Listed job", "running"))
        wait_done(self.jobs, jid)
        req = urllib.request.Request(self.base + "/api/log_download", data=b"{}", headers={"X-GQE": "1"}, method="POST")
        with urllib.request.urlopen(req) as r:
            self.assertIn("attachment", r.headers["Content-Disposition"])
            self.assertIn("second-marker", r.read().decode())


if __name__ == "__main__":
    unittest.main()
