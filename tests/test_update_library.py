import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import config, db, query, service
from gqe.cancel import Cancelled
from gqe.ingest import upsert_release
from gqe.depots import app_summary
from tests.test_gap_fill import app, catalog_runner, store_list_fake
from tests.test_sources import CFG, Fake, ctx_for, igdb_fake

IGDB_ONLY = {"igdb": {"client_id": "cid", "client_secret": "sec"}}


def big_db(n=60):
    conn = db.connect(":memory:")
    for i in range(n):
        upsert_release(conn, source="gog", source_key=str(i), name=f"Game {i}", platform="pc")
    return conn


class EmptyDatabaseGuard(unittest.TestCase):
    def test_refuses_on_a_near_empty_database_and_points_at_the_sources_below(self):
        conn = db.connect(":memory:")
        upsert_release(conn, source="gog", source_key="1", name="Only One", platform="pc")
        with self.assertRaises(service.DatabaseTooEmpty) as cm:
            service.update_library(conn, ctx_for(conn, Fake([]), {}), "new")
        self.assertIn("GOG catalog import", str(cm.exception))
        self.assertIsInstance(cm.exception, LookupError)                          # still caught by generic handling

    def test_runs_once_the_database_has_enough_in_it(self):
        conn = big_db(service.MIN_GAMES_FOR_UPDATE)                               # exactly at the line: should be allowed
        fake = Fake([("https://catalog.gog.com/v1/catalog", {"products": [], "productCount": 0})])
        out = service.update_library(conn, ctx_for(conn, fake, {}), "new")
        self.assertEqual(out["mode"], "new")


class NewOnly(unittest.TestCase):
    def test_pulls_new_games_from_whatever_is_configured_and_notes_the_rest(self):
        conn = big_db()
        gog_fake = [("https://catalog.gog.com/v1/catalog", {"products": [], "productCount": 0})]
        steam_fake_routes = store_list_fake([1]).routes
        fake = Fake(gog_fake + steam_fake_routes + [
            ("https://id.twitch.tv/oauth2/token", {"access_token": "t", "expires_in": 999999}),
            ("https://api.igdb.com/v4/games", lambda m, u, b: [{"id": 1}] if "game_type = 0" in (b or b"").decode() else []),
            ("https://api.igdb.com/v4/platforms", [{"id": 6, "name": "PC (Microsoft Windows)"}]),
        ])
        ctx = ctx_for(conn, fake, {**IGDB_ONLY, "steam": {"api_key": "k"}})
        ctx.services["pics_runner"] = catalog_runner([{"appid": 1, "status": "ok", "size": 1,
                                                        "summary": app_summary(app("Brand New Steam Game"))}])
        out = service.update_library(conn, ctx, "new")
        self.assertEqual(out["mode"], "new")
        self.assertIn("GOG catalog", out["results"])
        self.assertIn("IGDB new games", out["results"])
        self.assertIn("Steam new games", out["results"])
        self.assertNotIn("Fill in what's missing", out["results"])                # 'new' mode never fills gaps
        self.assertFalse(out["notes"])                                            # everything configured worked

    def test_unconfigured_sources_are_skipped_with_a_clear_note_not_a_crash(self):
        conn = big_db()
        fake = Fake([("https://catalog.gog.com/v1/catalog", {"products": [], "productCount": 0})])
        out = service.update_library(conn, ctx_for(conn, fake, {}), "new")        # no IGDB/Steam keys at all
        self.assertTrue(any("IGDB new games" in n and "not set up" in n for n in out["notes"]))
        self.assertTrue(any("Steam new games" in n and "not set up" in n for n in out["notes"]))
        self.assertIn("GOG catalog", out["results"])                              # GOG still ran; it needs no key

    def test_one_source_erroring_does_not_stop_the_others(self):
        conn = big_db()
        fake = Fake([("https://catalog.gog.com/v1/catalog", (500, {}, b"server error"))])
        out = service.update_library(conn, ctx_for(conn, fake, {}), "new")
        self.assertTrue(any("GOG catalog" in n and "stopped early" in n for n in out["notes"]))
        self.assertIsNone(out["results"]["GOG catalog"])


class NewAndFill(unittest.TestCase):
    def test_also_matches_to_steam_and_fills_gaps_and_tries_steamspy(self):
        conn = big_db(60)
        fake = Fake([
            ("https://catalog.gog.com/v1/catalog", {"products": [], "productCount": 0}),
            ("https://steamspy.com/api.php", (200, {}, b"")),
        ] + igdb_fake().routes)
        ctx = ctx_for(conn, fake, CFG)                                            # IGDB configured, no Steam key
        out = service.update_library(conn, ctx, "new_and_fill")
        self.assertIn("Match to Steam", out["results"])                           # IGDB alone is enough to attempt matching
        self.assertIn("Fill in what's missing", out["results"])
        self.assertIn("Steam review scores", out["results"])
        self.assertGreater(out["results"]["Fill in what's missing"]["asked"], 0)

    def test_igdb_import_platform_choice_is_remembered_for_update(self):
        """Just the config-persistence side effect, so this doesn't need a full two-platform import fake."""
        conn = big_db()
        os_ = __import__("os")
        os_.environ["GQE_HOME"] = str(Path(__file__).resolve().parent / "_tmp_cfgtest")
        try:
            fake = Fake([
                ("https://id.twitch.tv/oauth2/token", {"access_token": "t", "expires_in": 999999}),
                ("https://api.igdb.com/v4/platforms", [{"id": 6, "name": "PC (Microsoft Windows)"}, {"id": 8, "name": "PlayStation 2"}]),
                ("https://api.igdb.com/v4/games", lambda m, u, b: [{"id": 1}] if "game_type = 0" in (b or b"").decode() else []),
            ])
            service.update_source(conn, ctx_for(conn, fake, CFG), "igdb_import", {"platforms": ["pc", "ps2"]})
            self.assertEqual(sorted(config.load()["igdb"]["import_platforms"]), ["pc", "ps2"])
        finally:
            import shutil
            shutil.rmtree(os_.environ.pop("GQE_HOME"), ignore_errors=True)

    def test_cancellation_stops_between_steps_cleanly(self):
        conn = big_db()

        class StopAfterGog:
            calls = 0

            def __call__(self, msg, frac=None):
                StopAfterGog.calls += 1
                if StopAfterGog.calls > 1:                                        # let the first GOG step start, then stop
                    raise Cancelled()

        fake = Fake([("https://catalog.gog.com/v1/catalog", {"products": [], "productCount": 0})])
        ctx = ctx_for(conn, fake, {})
        with self.assertRaises(Cancelled):
            service.update_library(conn, ctx, "new_and_fill", StopAfterGog())

    def test_bad_mode_is_rejected(self):
        conn = big_db()
        with self.assertRaises(ValueError):
            service.update_library(conn, ctx_for(conn, Fake([]), {}), "sideways")


if __name__ == "__main__":
    unittest.main()
