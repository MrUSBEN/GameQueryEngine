"""Regression tests for three real bugs found in production use:
  1. gevent.Timeout is a BaseException, not an Exception - our retry loop only caught Exception,
     so a stale connection after ~1h crashed the whole job instead of being retried.
  2. SteamSpy signals "no more pages" with an EMPTY body, not empty JSON - we tried to parse it
     as JSON first and reported it as corruption.
  3. A crash mid-job could lose work already written but not yet committed.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import claims, db, query, service
from gqe.depots import app_summary
from gqe.http import Http
from gqe.ingest import upsert_release
from gqe.pics_worker import run as worker_run
from tests.test_gap_fill import app, catalog_runner, store_list_fake
from tests.test_sources import Fake, ctx_for


class GeventTimeout(BaseException):
    """Stands in for gevent.timeout.Timeout: a real BaseException, NOT an Exception, exactly like the
    one that crashed the worker in production. If our code catches `Exception` instead of `BaseException`
    for the retry, this propagates straight past it, same as it did for real."""


class FlakyClient:
    """Fails with a BaseException-derived error for the first N chunks, then works normally."""

    def __init__(self, fail_first_n_chunks=0, apps=None):
        self.fail_left, self.apps, self.calls = fail_first_n_chunks, apps or {}, 0

    def anonymous_login(self):
        return 1

    def get_product_info(self, apps=None, timeout=15):
        self.calls += 1
        if self.fail_left > 0:
            self.fail_left -= 1
            raise GeventTimeout("60 seconds")
        return {"apps": {a: self.apps[a] for a in apps if a in self.apps}}

    def logout(self):
        pass


class WorkerSurvivesBaseExceptionTimeouts(unittest.TestCase):
    def test_a_basexception_timeout_does_not_crash_the_worker(self):
        """This is the exact bug: catching only `Exception` lets this propagate and kill the process."""
        client = FlakyClient(fail_first_n_chunks=1, apps={1: app("A"), 2: app("B")})
        out = []
        code = worker_run([1, 2], out.append, lambda: client, batch=2)
        self.assertEqual(code, 0)                                                # did not crash
        statuses = {m["appid"]: m["status"] for m in out if "appid" in m}
        self.assertEqual(statuses, {1: "ok", 2: "ok"})                            # retried successfully

    def test_both_retries_failing_reports_error_not_unknown(self):
        """A real request failure must be distinguishable from Steam confirming 'no data for this app',
        so a transient hiccup doesn't permanently blacklist an app that was never actually checked."""
        client = FlakyClient(fail_first_n_chunks=2, apps={1: app("A")})           # both attempts fail
        out = []
        worker_run([1], out.append, lambda: client, batch=1)
        msg = next(m for m in out if "appid" in m)
        self.assertEqual(msg["status"], "error")

    def test_a_real_app_steam_has_no_data_for_is_still_unknown(self):
        client = FlakyClient(fail_first_n_chunks=0, apps={})                      # succeeds, but app 999 isn't in the reply
        out = []
        worker_run([999], out.append, lambda: client, batch=1)
        self.assertEqual(next(m for m in out if "appid" in m)["status"], "unknown")

    def test_system_exit_and_keyboard_interrupt_still_propagate(self):
        class Boom:
            def anonymous_login(self):
                return 1

            def get_product_info(self, apps=None, timeout=15):
                raise SystemExit(1)
        with self.assertRaises(SystemExit):
            worker_run([1], lambda m: None, lambda: Boom(), batch=1)

    def test_progress_reflects_a_grand_total_across_a_restart(self):
        client = FlakyClient(apps={i: app(f"g{i}") for i in range(1, 6)})
        out = []
        worker_run([4, 5], out.append, lambda: client, batch=2, progress_offset=3, progress_total=5)
        prog = next(m for m in out if "progress" in m)
        self.assertEqual(prog, {"progress": 5, "total": 5})                        # 3 (offset) + 2 done, out of 5 overall


class SubprocessRestart(unittest.TestCase):
    def test_default_restart_interval_is_well_under_the_observed_staleness_point(self):
        from gqe.adapters import steam_pics
        # production crash: ~15,200 apps in ~57 minutes before a session went stale. The default must
        # stay comfortably below that so a restart happens long before staleness can recur.
        self.assertLess(steam_pics.RESTART_EVERY, 8000)
        self.assertGreater(steam_pics.RESTART_EVERY, 100)                          # and not so small it's mostly overhead

    def test_worker_is_restarted_every_restart_every_apps_with_correct_offsets(self):
        from gqe.adapters import steam_pics
        calls = []

        def fake_run_worker(spec, progress):
            calls.append(spec)
            for aid in spec["appids"]:
                yield {"appid": aid, "status": "ok", "size": 1, "summary": app_summary(app(f"g{aid}"))}

        orig = steam_pics._run_worker
        steam_pics._run_worker = fake_run_worker
        try:
            old_restart = steam_pics.RESTART_EVERY
            steam_pics.RESTART_EVERY = 3
            appids = list(range(1, 8))                                            # 7 apps, restart every 3 -> 3 launches
            got = list(steam_pics._subprocess_runner(appids, lambda m, f=None: None))
            self.assertEqual(len(calls), 3)
            self.assertEqual([c["appids"] for c in calls], [[1, 2, 3], [4, 5, 6], [7]])
            self.assertEqual([c["progress_offset"] for c in calls], [0, 3, 6])
            self.assertTrue(all(c["progress_total"] == 7 for c in calls))
            self.assertEqual([g["appid"] for g in got], appids)                    # nothing dropped, nothing duplicated
        finally:
            steam_pics._run_worker = orig
            steam_pics.RESTART_EVERY = old_restart


class SteamCatalogErrorHandling(unittest.TestCase):
    def test_error_status_is_retried_later_not_permanently_blacklisted(self):
        conn = db.connect(":memory:")
        records = [{"appid": 1, "status": "error"},                                # a transient hiccup
                  {"appid": 2, "status": "unknown"}]                              # Steam genuinely has nothing
        asked = []
        c = ctx_for(conn, store_list_fake([1, 2]), {"steam": {"api_key": "k"}})
        c.services["pics_runner"] = catalog_runner(records, asked)
        out = service.steam_catalog(conn, c)
        self.assertEqual((out["retry_later"], out["not_available"]), (1, 1))
        self.assertEqual([r[0] for r in conn.execute("SELECT appid FROM steam_checked").fetchall()], [2])   # only app 2 recorded
        # a second run tries app 1 again (it's not in steam_checked) but leaves app 2 alone
        asked.clear()
        c2 = ctx_for(conn, store_list_fake([1, 2]), {"steam": {"api_key": "k"}})
        c2.services["pics_runner"] = catalog_runner(
            [{"appid": 1, "status": "ok", "size": 1, "summary": app_summary(app("Recovered"))}], asked)
        out2 = service.steam_catalog(conn, c2)
        self.assertEqual(asked, [1])
        self.assertEqual(out2["games_added"], 1)


class SteamSpyEmptyBody(unittest.TestCase):
    def test_a_truly_empty_body_is_the_end_of_the_list_not_an_error(self):
        conn = db.connect(":memory:")
        rid = upsert_release(conn, source="gog", source_key="1", name="A", platform="pc")
        conn.execute("INSERT INTO source_link(source,source_key,game_id,release_id) SELECT 'steam','5',game_id,id FROM releases WHERE id=?", (rid,))
        fake = Fake([("https://steamspy.com/api.php",
                      lambda m, u, b: {"5": {"appid": 5, "positive": 20, "negative": 0, "genre": "X"}} if "page=0" in u
                      else (200, {}, b""))])                                       # page 1: an empty body, not "{}"
        out = service.steamspy_scores(conn, ctx_for(conn, fake, {}))
        self.assertEqual(out["scores_saved"], 1)
        self.assertEqual(query.select(conn, [f"id={rid}"])[0]["score"], 100.0)
        self.assertEqual(query.count(conn, ["score!=unknown"]), 1)                 # completed cleanly, no error raised

    def test_genuinely_garbled_non_empty_text_still_raises_a_resumable_error(self):
        conn = db.connect(":memory:")
        fake = Fake([("https://steamspy.com/api.php", (200, {}, b"<html>not json at all</html>"))])
        with self.assertRaises(LookupError) as cm:
            service.steamspy_scores(conn, ctx_for(conn, fake, {}))
        self.assertIn("unreadable", str(cm.exception))

    def test_a_server_error_gives_a_resumable_message_not_a_crash(self):
        conn = db.connect(":memory:")
        fake = Fake([("https://steamspy.com/api.php", (503, {}, b"Service Unavailable"))])
        with self.assertRaises(LookupError) as cm:
            service.steamspy_scores(conn, ctx_for(conn, fake, {}))
        self.assertIn("Progress is saved", str(cm.exception))


class SteamSpyMalformedPages(unittest.TestCase):
    """Real report: SteamSpy consistently sent something unreadable at the same page every run."""

    def setUp(self):
        self.conn = db.connect(":memory:")
        rid1 = upsert_release(self.conn, source="gog", source_key="1", name="A", platform="pc")
        rid2 = upsert_release(self.conn, source="gog", source_key="2", name="B", platform="pc")
        self.conn.execute("INSERT INTO source_link(source,source_key,game_id,release_id) SELECT 'steam','10',game_id,id FROM releases WHERE id=?", (rid1,))
        self.conn.execute("INSERT INTO source_link(source,source_key,game_id,release_id) SELECT 'steam','20',game_id,id FROM releases WHERE id=?", (rid2,))
        self.rid1, self.rid2 = rid1, rid2

    def paged(self, pages):
        def handler(m, u, b):
            page = int(u.split("page=")[1].split("&")[0])
            return pages.get(page, (200, {}, b""))
        return Fake([("https://steamspy.com/api.php", handler)])

    def test_one_malformed_page_is_skipped_and_the_rest_still_completes(self):
        """This is the reported bug: page N is consistently unreadable, but pages before and after are fine."""
        pages = {
            0: {"10": {"appid": 10, "positive": 50, "negative": 0, "genre": "X"}},
            1: (200, {}, b'<br />\n<b>Notice</b>: Undefined index in api.php on line 42<br />\nnot json either'),
            2: {"20": {"appid": 20, "positive": 60, "negative": 0, "genre": "Y"}},
        }
        out = service.steamspy_scores(self.conn, ctx_for(self.conn, self.paged(pages), {}))
        self.assertEqual(out["bad_pages_skipped"], 1)
        self.assertEqual(out["scores_saved"], 2)                                    # neither real page was lost
        self.assertEqual(query.select(self.conn, [f"id={self.rid1}"])[0]["score"], 100.0)
        self.assertEqual(query.select(self.conn, [f"id={self.rid2}"])[0]["score"], 100.0)

    def test_php_notice_prepended_to_real_json_is_recovered(self):
        """A stray warning/notice before the real JSON body is a very common small-API bug; recover it
        instead of treating the whole page as garbage."""
        text = b'PHP Notice: something happened\n' + json.dumps({"10": {"appid": 10, "positive": 30, "negative": 0, "genre": "Z"}}).encode()
        pages = {0: (200, {}, text)}
        out = service.steamspy_scores(self.conn, ctx_for(self.conn, self.paged(pages), {}))
        self.assertEqual(out["bad_pages_skipped"], 0)                               # recovered, not counted as bad
        self.assertEqual(query.select(self.conn, [f"id={self.rid1}"])[0]["score"], 100.0)

    def test_persistent_corruption_still_gives_up_with_a_resumable_message(self):
        pages = {p: (200, {}, b"totally not json, every time") for p in range(0, 5)}
        with self.assertRaises(LookupError) as cm:
            service.steamspy_scores(self.conn, ctx_for(self.conn, self.paged(pages), {}))
        self.assertIn("3 pages in a row", str(cm.exception))
        self.assertIn("Progress is saved", str(cm.exception))

    def test_resuming_after_giving_up_starts_past_the_bad_run_not_from_page_0(self):
        ctx = ctx_for(self.conn, self.paged({p: (200, {}, b"bad") for p in range(10)}), {})
        with self.assertRaises(LookupError):
            service.steamspy_scores(self.conn, ctx)
        saved = ctx.state_get("steamspy", "progress")
        self.assertGreater(saved["page"], 0)                                        # not reset to the very start


class CommitOnAnyOutcome(unittest.TestCase):
    def test_a_crash_mid_job_still_keeps_work_already_written(self):
        import shutil
        import tempfile
        from gqe.server import Jobs

        tmp = Path(tempfile.mkdtemp())
        try:
            path = tmp / "t.db"
            db.connect(path).close()
            jobs = Jobs(path)

            def work(conn, progress):
                conn.execute("INSERT INTO games(name, name_norm, search_name) VALUES ('Saved', 'saved', 'saved')")
                progress("halfway", 0.5)
                raise RuntimeError("boom - simulates the worker crash")

            jid = jobs.start("Flaky job", work)
            import time
            for _ in range(200):
                if jobs.get(jid)["state"] != "running":
                    break
                time.sleep(0.01)
            self.assertEqual(jobs.get(jid)["state"], "error")
            conn = db.connect(path)
            self.assertEqual(conn.execute("SELECT name FROM games").fetchone()[0], "Saved")   # not lost
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
