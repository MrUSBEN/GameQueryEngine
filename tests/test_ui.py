import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class UiBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run(["node", str(ROOT / "tests" / "ui_harness.js"), str(ROOT / "gqe/static/index.html")],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr + r.stdout
        cls.out = json.loads(r.stdout.strip().splitlines()[-1])

    def test_page_boots_and_renders(self):
        o = self.out
        self.assertEqual(o["title"], {"name": "TestName", "ver": "v9.9.9"})       # name comes from the server (rename-safe)
        self.assertTrue(o["chipsRendered"])
        self.assertTrue(o["rowRendered"])

    def test_chip_cycles_include_exclude_clear(self):
        o = self.out
        self.assertEqual(o["afterOne"], [{"field": "platform", "op": "=", "value": ["pc"], "or_unknown": False}])
        self.assertEqual(o["afterTwo"], [{"field": "platform", "op": "!=", "value": ["pc"], "or_unknown": False}])
        self.assertEqual(o["sentTwo"], o["afterTwo"])                              # exactly this is sent to the server
        self.assertEqual(o["afterThree"], [])
        self.assertEqual(o["countText"], "1 active")
        self.assertTrue(o["clearEnabled"])

    def test_every_filter_kind_can_be_negated(self):
        got = {(f["field"], f["op"]): f["value"] for f in self.out["mixed"]}
        self.assertEqual(got[("genre", "!=")], ["indie"])
        self.assertEqual(got[("region", "!~")], ["USA"])
        self.assertEqual(got[("drm", "=")], ["drm-free"])
        self.assertEqual(got[("name", "!~")], "demo, beta")
        self.assertEqual(got[("fav", "=")], "no")                                   # hide favourites
        self.assertEqual(got[("size", "<=")], "2GB")

    def test_clear_all_returns_to_default(self):
        c = self.out["afterClear"]
        self.assertEqual(c["filters"], [])
        self.assertEqual((c["count"], c["disabled"], c["qx"], c["fav"]), ("none active", True, "", ""))

    def test_column_select_all_none_reset(self):
        o = self.out
        self.assertEqual(o["colsNone"], 0)
        self.assertEqual(o["colsAll"], 15)
        self.assertEqual(o["colsDefault"], ["name", "platform", "region", "year", "size_gb", "score", "price", "drm", "genres"])

    def test_refresh_all_columns_mode(self):
        o = self.out
        self.assertEqual(o["allMode"], {"checked": True, "disabled": True})         # 'all' always means fill-empty-only
        self.assertEqual(o["estBody"], {"field": "all", "only_missing": True})
        self.assertIn("5 of 8 games need looking up", o["estText"])
        self.assertEqual(o["singleMode"], {"disabled": False})                       # single column: your choice

    def test_mark_all_filtered_and_selected(self):
        o = self.out
        self.assertEqual(o["markAll"], {"filters": [], "flag": "played", "value": True})     # nothing selected: all results
        self.assertEqual(o["toast"], "3 games marked as played")
        self.assertEqual(o["markSelected"], {"ids": [1], "flag": "favorite", "value": True})  # selection only

    def test_row_played_tick(self):
        self.assertTrue(self.out["rowHasTick"])
        self.assertEqual(self.out["tickClick"], {"game_id": 7, "flag": "played", "value": True})

    def test_steam_size_steps(self):
        o = self.out
        self.assertIn("Linked 3,000 games to Steam (2,900 by IGDB's exact IDs, 100 by title", o["step1"])
        self.assertIn("50 ambiguous and 1,940 unmatched", o["step1"])                    # nothing guessed, and the user is told
        self.assertEqual(o["step2"]["sent"]["source"], "steam_probe")
        self.assertEqual(o["step2"]["sent"]["options"], {"count": 200})
        self.assertEqual(o["step2"]["msg"], "Test finished. Nothing was saved.")
        self.assertIn("Looks good (75% got a size)", o["step2"]["html"])
        self.assertIn("1.4×", o["step2"]["html"])
        self.assertIn("Took 12.5 s (16 games/s", o["step2"]["html"])                     # timing is now reported...
        self.assertIn("31 minutes", o["step2"]["html"])                                  # ...with a projection for everything linked
        self.assertIn("2 were not games", o["step2"]["html"])
        self.assertIn("Half-Life", o["step2"]["html"])
        self.assertEqual(o["step3"], "Saved 1,234 sizes.")
        self.assertEqual(o["addon"], {"text": "not installed", "installHidden": False})

    def test_optional_steam_key_and_hint(self):
        o = self.out
        self.assertIn("A Steam key (optional) can link more", o["step1IgdbOnly"])         # only when it would actually help
        self.assertEqual(o["keySent"], {"steam": {"api_key": "MYKEY"}})
        self.assertEqual(o["keyCleared"], "")                                             # the secret never stays in the page
        self.assertEqual(o["keyStatus"], "✓ key saved")

    def test_live_log_tab(self):
        o = self.out
        self.assertEqual((o["log"]["tabShown"], o["log"]["browseHidden"]), ("flex", "none"))
        self.assertIn("Job 1 started: Update steam_match", o["log"]["html"])
        self.assertIn('class="ll WARNING"', o["log"]["html"])
        self.assertIn('class="ll ERROR"', o["log"]["html"])
        self.assertEqual(o["log"]["status"], "3 lines")
        self.assertEqual(o["logNextPoll"], "/api/log?since=3")                            # only new lines are fetched
        self.assertNotIn("started", o["logWarnOnly"])                                     # warnings-only view
        self.assertIn("retry 1/3", o["logWarnOnly"])
        self.assertIn("Job 1 failed", o["logWarnOnly"])

    def test_header_pill_shows_running_work(self):
        p = self.out["pill"]
        self.assertFalse(p["hidden"])
        self.assertIn("Steam depot sizes: 1,200/29,000 games", p["text"])

    def test_cancel_button(self):
        o = self.out
        self.assertEqual(o["cancelShown"], {"header": True, "log": True})                 # visible while a job runs
        self.assertEqual(o["cancelSent"], {"id": 1})
        self.assertIn("everything saved so far is kept", o["cancelToast"])
        self.assertTrue(o["cancelling"]["pill"].startswith("◌ Cancelling"))
        self.assertTrue(o["cancelling"]["headerHidden"] and o["cancelling"]["logHidden"])  # can't be pressed twice

    def test_cancelled_job_is_reported_calmly_and_data_refreshes(self):
        c = self.out["cancelledMsg"]
        self.assertEqual(c["text"], "Cancelled. Everything saved before you cancelled was kept.")
        self.assertEqual(c["cls"], "msg")                                                  # neutral, not a red error
        self.assertTrue(c["refreshed"])                                                    # the table reloads to show what was kept

    def test_steam_catalog_and_steamspy_reports(self):
        self.assertIn("added 3,000 new games", self.out["catalogMsg"])
        self.assertIn("skipped 8,000 non-games", self.out["catalogMsg"])
        self.assertIn("4,000 scores", self.out["spyMsg"])
        self.assertIn("900 games had too few reviews", self.out["spyMsg"])

    def test_rawg_key_is_saved_and_never_echoed(self):
        self.assertEqual(self.out["rawgKeySent"], {"rawg": {"api_key": "RAWGKEY"}})
        self.assertEqual(self.out["rawgKeyCleared"], "")
        self.assertIn("8,000", self.out["rawgMsg"])

    def test_status_tab_shows_missing_counts_examples_and_color_coding(self):
        s = self.out["status"]
        self.assertEqual(s["tabShown"], "flex")
        self.assertIn("10 missing (10%)", s["platformsHtml"])
        self.assertIn("Unknown Game A", s["platformsHtml"])                          # example titles keep their real case
        self.assertIn("complete", s["platformsHtml"])                                # a field with 0 missing says so plainly
        self.assertIn('class="chit red"', s["platformsHtml"])                        # problematic fields are flagged red
        self.assertIn("PC", s["overviewHtml"])                                       # the moved "what's in your database" table
        self.assertIn("gog", s["sourcesHtml"])                                       # per-source activity

    def test_update_new_only_shows_a_per_step_summary(self):
        u = self.out["updateNew"]
        self.assertEqual(u["sentMode"], "new")
        self.assertIn("GOG catalog: 500 read, 20 new, 480 already had", u["out"])
        self.assertIn("skipped (not set up yet)", u["out"])


if __name__ == "__main__":
    unittest.main()
