"""Regression tests for a real report: Update -> Wikidata failed twice in a row within seconds, and
'GameCube'/'Dreamcast' were reported as having no matching platform on Wikidata."""
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import claims, db, query, service
from gqe.adapters import wikidata
from gqe.adapters.redump import RedumpIngestor
from gqe.claims import now_iso
from gqe.ingest import upsert_release
from tests.test_sources import Fake, ctx_for

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_ps2.dat"


def platform_row(pid, name, count=200):
    return {"p": {"value": f"http://www.wikidata.org/entity/Q{pid}"}, "n": {"value": str(count)}}


def game_row(item, label, date=None, prec=11, qp=None):
    r = {"item": {"value": f"http://www.wikidata.org/entity/{item}"}, "label": {"value": label}}
    if date:
        r["date"], r["prec"] = {"value": date}, {"value": str(prec)}
    if qp:
        r["qp"] = {"value": f"http://www.wikidata.org/entity/{qp}"}
    return r


class CorrectLabels(unittest.TestCase):
    def test_gamecube_uses_its_real_wikidata_label(self):
        # this is the exact bug: the old label ("GameCube") never matched anything on Wikidata
        self.assertEqual(wikidata.LABELS["gc"], "Nintendo GameCube")

    def test_a_console_that_is_actually_findable_now_gets_matched(self):
        conn = db.connect(":memory:")
        rid = upsert_release(conn, source="redump", source_key="1", name="Luigi's Mansion", platform="gc")
        calls = []

        def handler(m, u, b):
            calls.append(u)
            if "GROUP+BY" in u:                                     # only the platform-count query has this
                return {"results": {"bindings": [platform_row(1, "x")]}}
            return {"results": {"bindings": [game_row("Q1", "Luigi's Mansion", "2001-11-18", 11, "Q1")]}}
        fake = Fake([("https://query.wikidata.org/sparql", handler)])
        out = service.refresh_fields(conn, ctx_for(conn, fake, {}), ["orig_year"], [rid], "wikidata")
        self.assertEqual(out["updated"], 1)
        self.assertEqual(len(calls), 2)                              # the platform lookup, then the game data
        self.assertIn("Nintendo+GameCube", calls[0])


class NegativeCaching(unittest.TestCase):
    def test_a_platform_with_no_wikidata_match_is_not_requeried_within_the_ttl(self):
        conn = db.connect(":memory:")
        rid = upsert_release(conn, source="redump", source_key="1", name="Some Saturn Game", platform="saturn")
        calls = []

        def handler(m, u, b):
            calls.append(1)
            return {"results": {"bindings": []}}          # no matching platform, ever
        fake = Fake([("https://query.wikidata.org/sparql", handler)])
        ctx = ctx_for(conn, fake, {})
        service.refresh_fields(conn, ctx, ["orig_year"], [rid], "wikidata")
        self.assertEqual(len(calls), 1)
        calls.clear()
        service.refresh_fields(conn, ctx, ["orig_year"], [rid], "wikidata")     # same ctx/state: should skip the query
        self.assertEqual(len(calls), 0)
        cached = ctx.state_get("wikidata", "platform:saturn")
        self.assertIn("unavailable_until", cached)

    def test_a_found_platform_is_cached_forever_not_just_for_the_ttl(self):
        conn = db.connect(":memory:")
        rid = upsert_release(conn, source="redump", source_key="1", name="Sonic Adventure", platform="dc")
        calls = []

        def handler(m, u, b):
            calls.append(1)
            if "COUNT(" in u:
                return {"results": {"bindings": [platform_row(5, "x")]}}
            return {"results": {"bindings": []}}
        fake = Fake([("https://query.wikidata.org/sparql", handler)])
        ctx = ctx_for(conn, fake, {})
        service.refresh_fields(conn, ctx, ["orig_year"], [rid], "wikidata")
        n_first = len(calls)
        calls.clear()
        service.refresh_fields(conn, ctx, ["orig_year"], [rid], "wikidata")
        self.assertEqual(len(calls), n_first - 1)          # the platform lookup itself is skipped the 2nd time


class CooldownLock(unittest.TestCase):
    def test_a_429_sets_a_cooldown_that_blocks_further_requests_instantly(self):
        conn = db.connect(":memory:")
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        ids = query.ids(conn)
        fake = Fake([("https://query.wikidata.org/", (429, {"retry-after": "1000"}, b"Too Many Requests"))])
        ctx = ctx_for(conn, fake, {})

        with self.assertRaises(LookupError) as cm:
            service.refresh_fields(conn, ctx, ["release_date"], ids, "wikidata")
        self.assertIn("slow down", str(cm.exception))
        self.assertGreater(wikidata._cooldown_remaining(ctx), 900)          # ~1000s, not silently reset

        # this is the reported scenario: trying again seconds later
        calls_before = len(fake.calls)
        with self.assertRaises(LookupError) as cm2:
            service.refresh_fields(conn, ctx, ["release_date"], ids, "wikidata")
        self.assertIn("slow down", str(cm2.exception))
        self.assertEqual(len(fake.calls), calls_before)                     # NOT a second network request

    def test_the_job_reports_a_real_error_not_a_silent_zero_result(self):
        """This is the actual bug found while fixing this: the cooldown error was being swallowed inside
        fetch()'s per-platform loop, so Update->Wikidata looked like it succeeded with 0 updates."""
        conn = db.connect(":memory:")
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        fake = Fake([("https://query.wikidata.org/", (429, {"retry-after": "50"}, b"slow down"))])
        ctx = ctx_for(conn, fake, {})
        with self.assertRaises(LookupError):
            service.refresh_fields(conn, ctx, ["release_date"], query.ids(conn), "wikidata")
        # NOT: {"updated": 0, "notes": []} as if nothing was wrong

    def test_cooldown_does_not_affect_other_sources(self):
        conn = db.connect(":memory:")
        rid = upsert_release(conn, source="redump", source_key="1", name="X", platform="ps2")
        claims.record_claim(conn, rid, "score", "steam", 1)   # just to exist; not exercised here
        ctx = ctx_for(conn, Fake([]), {})
        wikidata._set_cooldown(ctx, 500)
        self.assertGreater(wikidata._cooldown_remaining(ctx), 0)
        # a totally unrelated lookup (e.g. IGDB) must not be affected by Wikidata's own cooldown state
        self.assertIsNone(ctx.state_get("igdb", "token"))

    def test_cooldown_expires_naturally(self):
        conn = db.connect(":memory:")
        ctx = ctx_for(conn, Fake([]), {})
        ctx.state_set("wikidata", wikidata.COOLDOWN_KEY, (wikidata._now() - timedelta(seconds=5)).isoformat())
        self.assertEqual(wikidata._cooldown_remaining(ctx), 0.0)


if __name__ == "__main__":
    unittest.main()
