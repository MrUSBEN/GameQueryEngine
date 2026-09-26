import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import adapters, db, logs, query, service
from gqe.adapters import Context
from gqe.adapters.redump import RedumpIngestor
from gqe.http import Http, HttpError
from gqe.server import Jobs
from tests.test_sources import Fake, ctx_for

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_ps2.dat"


class LongWaits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logs.setup(console=False, file=False)

    def test_a_1000_second_retry_after_fails_fast_with_a_clear_message(self):
        slept = []
        http = Http(transport=Fake([("https://query.wikidata.org/", (429, {"retry-after": "1000"}, b"Too Many Requests"))]),
                    sleep=slept.append)
        t0 = time.monotonic()
        with self.assertRaises(HttpError) as cm:
            http.get_json("https://query.wikidata.org/sparql", params={"query": "x"})
        self.assertLess(time.monotonic() - t0, 1)
        self.assertEqual(slept, [])                                              # it did NOT sleep for 16 minutes
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (429, 1000.0))
        self.assertIn("won't wait that long", str(cm.exception))
        self.assertIn("17 minutes", str(cm.exception))

    def test_short_retry_after_is_still_honoured(self):
        slept, n = [], {"c": 0}

        def flaky(m, u, b):
            n["c"] += 1
            return (429, {"retry-after": "7"}, b"slow") if n["c"] == 1 else {"ok": 1}
        Http(transport=Fake([("https://x.test/", flaky)]), sleep=slept.append).get_json("https://x.test/a")
        self.assertIn(7.0, slept)

    def test_waiting_for_a_rate_limit_can_be_cancelled(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            path = tmp / "t.db"
            db.connect(path).close()
            http = Http(transport=Fake([("https://slow.test/", {"ok": 1})]), intervals={"slow.test": 60.0})   # real sleeping
            jobs = Jobs(path)

            def work(conn, progress):
                http.get_json("https://slow.test/a")
                http.get_json("https://slow.test/b")                                                            # must wait ~60 s
                return {"finished": True}
            jid = jobs.start("Waiting job", work)
            time.sleep(0.6)
            jobs.cancel(jid)
            t0 = time.monotonic()
            while jobs.get(jid)["state"] == "running" and time.monotonic() - t0 < 4:
                time.sleep(0.05)
            self.assertEqual(jobs.get(jid)["state"], "cancelled")
            self.assertLess(time.monotonic() - t0, 3)                                                           # not 60 s
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SourceIsolation(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        service.build_from_records(self.conn, RedumpIngestor(SAMPLE))

        class Broken(adapters.Refresher):
            name, fields = "aaa_broken", frozenset({"score"})

            def fetch(self, rows, fields, links):
                raise HttpError(429, "slow down", "https://broken.test/x")
                yield

        class Works(adapters.Refresher):
            name, fields = "bbb_works", frozenset({"score"})

            def fetch(self, rows, fields, links):
                for r in rows:
                    yield adapters.Update(r["id"], "score", 77.0)
        adapters.REFRESHERS["aaa_broken"], adapters.REFRESHERS["bbb_works"] = Broken, Works

    def tearDown(self):
        adapters.REFRESHERS.pop("aaa_broken", None)
        adapters.REFRESHERS.pop("bbb_works", None)

    def test_automatic_mode_keeps_going_when_one_source_fails(self):
        ids = query.ids(self.conn)
        out = service.refresh_fields(self.conn, ctx_for(self.conn, Fake([]), {}), ["score"], ids)
        self.assertGreater(out["updated"], 0)
        self.assertEqual(query.count(self.conn, ["score=77"]), len(ids))          # the working source still delivered everything
        self.assertTrue(any("aaa_broken stopped" in n and "429" in n for n in out["notes"]))

    def test_explicit_source_reports_its_failure_plainly(self):
        with self.assertRaises(HttpError):
            service.refresh_fields(self.conn, ctx_for(self.conn, Fake([]), {}), ["score"], query.ids(self.conn), "aaa_broken")


class WikidataIsExplicitOnly(unittest.TestCase):
    def test_not_part_of_automatic_but_available_when_chosen(self):
        conn = db.connect(":memory:")
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        ids = query.ids(conn)
        auto = service.estimate_refresh(conn, ctx_for(conn, Fake([]), {}), ["release_date", "orig_year"], ids)
        self.assertNotIn("wikidata", auto["sources"])
        chosen = service.estimate_refresh(conn, ctx_for(conn, Fake([]), {}), ["release_date"], ids, "wikidata")
        self.assertEqual(chosen["sources"], ["wikidata"])

    def test_wikidata_throttle_becomes_a_readable_error(self):
        conn = db.connect(":memory:")
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        fake = Fake([("https://query.wikidata.org/", (429, {"retry-after": "1000"}, b"Too Many Requests"))])
        ctx = Context(conn, Http(transport=fake, sleep=lambda s: None), {})
        # the underlying HTTP layer still fails fast (this is what that guarantees)...
        with self.assertRaises(HttpError) as cm:
            ctx.http.get_json("https://query.wikidata.org/sparql", params={"query": "x"})
        self.assertIn("won't wait that long", str(cm.exception))
        # ...and the Wikidata source turns that into a plain, resumable message, not a raw HTTP error
        with self.assertRaises(LookupError) as cm:
            service.refresh_fields(conn, ctx, ["release_date"], query.ids(conn), "wikidata")
        self.assertIn("slow down", str(cm.exception))
        self.assertNotIsInstance(cm.exception, HttpError)


if __name__ == "__main__":
    unittest.main()
