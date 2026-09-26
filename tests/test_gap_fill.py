import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import claims, db, query, service
from gqe.adapters.redump import RedumpIngestor
from gqe.depots import app_summary, genres_from_ids
from gqe.ingest import upsert_release
from tests.test_sources import Fake, ctx_for
from tests.test_steam_sizes import runner_for

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_ps2.dat"


def ts(y, m=1, d=1):
    from datetime import datetime, timezone
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


def app(name, type_="game", genre_ids=(1, 23), original=None, steam=None, metacritic=None, windows=True):
    common = {"name": name, "type": type_, "genres": {str(i): str(g) for i, g in enumerate(genre_ids)}}
    if original:
        common["original_release_date"] = str(original)
    if steam:
        common["steam_release_date"] = str(steam)
    if metacritic:
        common["metacritic_score"] = str(metacritic)
    if not windows:
        common["oslist"] = "linux"
    return {"common": common, "depots": {"1": {"manifests": {"public": {"size": "1000000000", "download": "1"}}}}}


class Summary(unittest.TestCase):
    def test_extracts_the_fields_the_catalog_needs(self):
        s = app_summary(app("Half-Life", original=ts(1998, 11, 19), metacritic=96))
        self.assertEqual((s["name"], s["type"], s["windows"], s["metacritic"]), ("Half-Life", "game", True, 96))
        self.assertEqual(genres_from_ids(s["genre_ids"]), ["action", "indie"])
        self.assertFalse(app_summary(app("x", windows=False))["windows"])
        self.assertEqual(genres_from_ids(["1", "999", "23"]), ["action", "indie"])            # unknown ids ignored


def catalog_runner(records, asked=None):
    def runner(appids, progress):
        if asked is not None:
            asked.extend(appids)
        by = {r["appid"]: r for r in records}
        for a in appids:
            r = by.get(a)
            if r:
                yield r
    return runner


def store_list_fake(ids):
    return Fake([("https://api.steampowered.com/IStoreService/GetAppList/v1/",
                  {"response": {"apps": [{"appid": i, "name": f"app{i}"} for i in ids], "have_more_results": False}})])


class SteamCatalog(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def ctx(self, records=None, ids=None, asked=None, key="STEAMKEY"):
        c = ctx_for(self.conn, store_list_fake(ids or []), {"steam": {"api_key": key}} if key else {})
        if records is not None:
            c.services["pics_runner"] = catalog_runner(records, asked)
        return c

    def test_adds_new_games_and_links_existing_ones(self):
        existing = upsert_release(self.conn, source="gog", source_key="1", name="Portal", platform="pc")
        records = [
            {"appid": 400, "status": "ok", "size": 1_500_000_000, "summary": app_summary(app("Portal", metacritic=90, original=ts(2007, 10, 9)))},
            {"appid": 500, "status": "ok", "size": 2_000_000_000, "summary": app_summary(app("New Game", genre_ids=(3,)))},
            {"appid": 501, "status": "not_a_game", "summary": app_summary(app("Some DLC", "dlc"))},
            {"appid": 502, "status": "no_depots", "summary": app_summary(app("No Windows Build", windows=False))},
            {"appid": 503, "status": "unknown"},
        ]
        out = service.steam_catalog(self.conn, self.ctx(records, ids=[x["appid"] for x in records]))
        self.assertEqual((out["games_added"], out["linked_to_existing"], out["skipped_not_games"], out["not_available"]), (1, 1, 2, 1))
        p = query.select(self.conn, [f"id={existing}"])[0]
        self.assertEqual((p["score"], p["size_bytes"], p["drm"]), (90.0, 1_500_000_000, "steam"))
        self.assertEqual(p["orig_year"], 2007)
        new = query.select(self.conn, ['name="New Game"'])[0]
        self.assertEqual(new["genres"], "|rpg|")
        self.assertEqual(query.count(self.conn, ["platform=pc"]), 2)                          # Portal (existing, now linked) + New Game; not the DLC or the unknown app
        self.assertEqual(conn_checked(self.conn, 501), "dlc")
        self.assertEqual(conn_checked(self.conn, 503), "unknown")

    def test_rerun_skips_everything_already_settled(self):
        records = [{"appid": 1, "status": "ok", "size": 1, "summary": app_summary(app("A"))},
                   {"appid": 2, "status": "not_a_game", "summary": app_summary(app("B", "dlc"))}]
        asked = []
        service.steam_catalog(self.conn, self.ctx(records, ids=[1, 2], asked=asked))
        asked.clear()
        out = service.steam_catalog(self.conn, self.ctx(records, ids=[1, 2], asked=asked))
        self.assertEqual(asked, [])                                                            # nothing left to check
        self.assertEqual(out["to_check"], 0)

    def test_same_title_different_release_year_is_kept_separate(self):
        old = upsert_release(self.conn, source="gog", source_key="1", name="Recompile", platform="pc")
        claims.record_claim(self.conn, old, "orig_year", "manual", 1995)
        records = [{"appid": 1, "status": "ok", "size": 1, "summary": app_summary(app("Recompile", original=ts(2021, 6, 1)))}]
        out = service.steam_catalog(self.conn, self.ctx(records, ids=[1]))
        self.assertEqual((out["games_added"], out["different_game_same_title"]), (1, 1))
        self.assertEqual(query.count(self.conn, ["name=Recompile"]), 2)

    def test_needs_the_addon(self):
        from gqe.adapters import steam_pics
        orig = steam_pics.addon_installed
        steam_pics.addon_installed = lambda: False
        try:
            with self.assertRaises(LookupError) as cm:
                service.steam_catalog(self.conn, self.ctx(ids=[1]))     # even with a key, the add-on check runs first
            self.assertIn("add-on", str(cm.exception))
        finally:
            steam_pics.addon_installed = orig

    def test_needs_a_steam_key_and_says_why(self):
        with self.assertRaises(LookupError) as cm:
            service.steam_catalog(self.conn, self.ctx(ids=[1], key=None))
        self.assertIn("free Steam key", str(cm.exception))
        self.assertIn("no keyless way", str(cm.exception))

    def test_app_list_is_cached_between_runs(self):
        fake = store_list_fake([1])
        c = ctx_for(self.conn, fake, {"steam": {"api_key": "k"}})
        c.services["pics_runner"] = catalog_runner([{"appid": 1, "status": "ok", "size": 1, "summary": app_summary(app("A"))}])
        service.steam_catalog(self.conn, c)
        service.steam_catalog(self.conn, c)
        self.assertEqual(fake.count("https://api.steampowered.com/IStoreService"), 1)   # the list is not re-fetched


def conn_checked(conn, appid):
    return conn.execute("SELECT kind FROM steam_checked WHERE appid=?", (appid,)).fetchone()[0]


def steamspy_page(entries, start_page=0):
    """Serves `entries` at `start_page`, empty pages after (stopping the loop). Any request for an earlier
    page is a test bug (resume must not re-fetch pages already processed)."""
    def handler(method, url, body):
        page = int(url.split("page=")[1].split("&")[0])
        assert page >= start_page, f"resume re-fetched page {page}, expected to start at {start_page}"
        return entries if page == start_page else {}
    return handler


class SteamSpy(unittest.TestCase):
    def test_scores_saved_only_with_enough_reviews_and_paging_stops_on_empty(self):
        conn = db.connect(":memory:")
        good = upsert_release(conn, source="gog", source_key="1", name="Popular", platform="pc")
        thin = upsert_release(conn, source="gog", source_key="2", name="Niche", platform="pc")
        untracked = upsert_release(conn, source="gog", source_key="3", name="Not Linked", platform="pc")
        conn.execute("INSERT INTO source_link(source,source_key,game_id,release_id) SELECT 'steam','100',game_id,id FROM releases WHERE id=?", (good,))
        conn.execute("INSERT INTO source_link(source,source_key,game_id,release_id) SELECT 'steam','101',game_id,id FROM releases WHERE id=?", (thin,))
        entries = {"100": {"appid": 100, "positive": 90, "negative": 10, "genre": "Action, Indie"},
                   "101": {"appid": 101, "positive": 2, "negative": 1, "genre": "Puzzle"}}
        fake = Fake([("https://steamspy.com/api.php", steamspy_page(entries))])
        out = service.steamspy_scores(conn, ctx_for(conn, fake, {}))
        self.assertEqual((out["scores_saved"], out["too_few_reviews"]), (1, 1))
        self.assertEqual(query.select(conn, [f"id={good}"])[0]["score"], 90.0)
        self.assertIsNone(query.select(conn, [f"id={thin}"])[0]["score"])
        self.assertIsNone(query.select(conn, [f"id={untracked}"])[0]["score"])
        self.assertEqual(query.select(conn, [f"id={good}"])[0]["genres"], "|action|indie|")

    def test_resumes_from_the_saved_page_after_an_interruption(self):
        conn = db.connect(":memory:")
        rid = upsert_release(conn, source="gog", source_key="1", name="Resumed", platform="pc")
        conn.execute("INSERT INTO source_link(source,source_key,game_id,release_id) SELECT 'steam','777',game_id,id FROM releases WHERE id=?", (rid,))
        ctx = ctx_for(conn, Fake([]), {})
        ctx.state_set("steamspy", "progress", {"page": 3, "at": claims.now_iso()})
        entries = {"777": {"appid": 777, "positive": 10, "negative": 0, "genre": "Filler"}}
        fake = Fake([("https://steamspy.com/api.php", steamspy_page(entries, start_page=3))])
        out = service.steamspy_scores(conn, _swap_http(ctx, fake))
        self.assertEqual(out["resumed_from_page"], 3)
        self.assertEqual(query.select(conn, [f"id={rid}"])[0]["score"], 100.0)


def _swap_http(ctx, fake):
    from gqe.http import Http
    ctx.http = Http(transport=fake, sleep=lambda s: None)
    return ctx


class SteamSpyRateLimit(unittest.TestCase):
    def test_steamspy_is_limited_to_about_one_request_per_minute(self):
        from gqe.http import DEFAULT_INTERVALS
        self.assertGreaterEqual(DEFAULT_INTERVALS["steamspy.com"], 55)          # SteamSpy's own documented limit


class Rawg(unittest.TestCase):
    def rawg_fake(self):
        from tests.test_sources import Fake
        return Fake([
            ("https://api.rawg.io/api/platforms", {"results": [{"id": 15, "name": "PlayStation 2"}], "next": None}),
            ("https://api.rawg.io/api/games", {"results": [
                {"id": 1, "name": "Katamari Damacy", "released": "2004-03-18", "metacritic": 85,
                 "genres": [{"name": "Puzzle"}], "ratings_count": 500,
                 "platforms": [{"platform": {"id": 15}, "released_at": "2004-09-24"}]},
                {"id": 2, "name": "Obscure Game", "released": "2003-01-01", "rating": 4.2, "ratings_count": 50, "genres": []},
                {"id": 3, "name": "Barely Rated", "released": "2002-01-01", "rating": 3.0, "ratings_count": 2, "genres": []},
            ], "next": None}),
        ])

    def test_console_games_get_year_genre_and_score(self):
        conn = db.connect(":memory:")
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        obscure = upsert_release(conn, source="redump", source_key="obscure", name="Obscure Game", platform="ps2")
        barely = upsert_release(conn, source="redump", source_key="barely", name="Barely Rated", platform="ps2")
        ids = query.ids(conn)
        ctx = ctx_for(conn, self.rawg_fake(), {"rawg": {"api_key": "k"}})
        out = service.refresh_fields(conn, ctx, ["release_date", "orig_year", "genres", "score"], ids, "rawg")
        self.assertGreater(out["updated"], 0)
        k = query.select(conn, ["name~katamari", "region=USA"])[0]
        self.assertEqual((k["release_date"], k["orig_year"], k["score"], k["genres"]), ("2004-09-24", 2004, 85.0, "|puzzle|"))
        self.assertEqual(query.select(conn, [f"id={obscure}"])[0]["score"], 84.0)              # 4.2 * 20, enough ratings
        self.assertIsNone(query.select(conn, [f"id={barely}"])[0]["score"])                    # too few ratings: no score, never guessed

    def test_needs_a_key_and_reports_rejection_clearly(self):
        conn = db.connect(":memory:")
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        with self.assertRaises(LookupError) as cm:
            service.refresh_fields(conn, ctx_for(conn, self.rawg_fake(), {}), ["score"], query.ids(conn), "rawg")
        self.assertIn("needs setup", str(cm.exception))
        bad = Fake([("https://api.rawg.io/api/platforms", (401, {}, b"Unauthorized"))])
        with self.assertRaises(LookupError) as cm:
            service.refresh_fields(conn, ctx_for(conn, bad, {"rawg": {"api_key": "wrong"}}), ["score"], query.ids(conn), "rawg")
        self.assertIn("rejected the API key", str(cm.exception))

    def test_monthly_allowance_is_respected(self):
        conn = db.connect(":memory:")
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        ctx = ctx_for(conn, self.rawg_fake(), {"rawg": {"api_key": "k"}})
        from gqe.adapters.rawg import RawgRefresher
        ctx.state_set("rawg", RawgRefresher(ctx)._usage_key(), 18_000)
        with self.assertRaises(LookupError) as cm:
            service.refresh_fields(conn, ctx, ["score"], query.ids(conn), "rawg")
        self.assertIn("almost used", str(cm.exception))

    def test_automatic_mode_does_not_include_rawg_it_needs_a_key(self):
        conn = db.connect(":memory:")
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        est = service.estimate_refresh(conn, ctx_for(conn, Fake([]), {}), ["score"], query.ids(conn))
        self.assertNotIn("rawg", est["sources"])


class GapsReport(unittest.TestCase):
    def test_reports_missing_counts_and_examples_per_platform(self):
        conn = db.connect(":memory:")
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        pc_full = upsert_release(conn, source="gog", source_key="1", name="Complete PC Game", platform="pc",
                                 size_bytes=1, size_conf="exact")
        claims.record_claim(conn, pc_full, "release_date", "gog", "2020-01-01")
        claims.record_claim(conn, pc_full, "score", "gog", 80)
        claims.record_claim(conn, pc_full, "genres", "gog", "action")
        upsert_release(conn, source="gog", source_key="2", name="Gappy PC Game", platform="pc")
        report = service.gaps_report(conn)
        pc = next(p for p in report if p["platform"] == "pc")
        self.assertEqual(pc["columns"]["year"]["missing"], 1)
        self.assertIn("Gappy PC Game", pc["columns"]["year"]["examples"])
        self.assertEqual(pc["columns"]["size"]["missing"], 1)
        ps2 = next(p for p in report if p["platform"] == "ps2")
        self.assertEqual(ps2["columns"]["year"]["missing"], 8)                                 # redump has no dates
        self.assertNotIn("size", ps2["columns"])                                               # ps2 sizes are already exact; not a gap worth showing


if __name__ == "__main__":
    unittest.main()
