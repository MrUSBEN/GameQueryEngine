import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import claims, db, query, service
from gqe.adapters import steam_pics
from gqe.depots import compute_install_size
from gqe.ingest import upsert_release
from gqe.pics_worker import run as worker_run
from tests.test_sources import Fake, ctx_for, steam_fake

TS_2002 = int(datetime(2002, 4, 1, tzinfo=timezone.utc).timestamp())

APP = {"common": {"original_release_date": str(TS_2002)}, "depots": {
    "100": {"config": {"oslist": "windows"}, "manifests": {"public": {"gid": "1", "size": "5000000000", "download": "2000000000"}}},
    "101": {"config": {"oslist": "linux"}, "manifests": {"public": {"gid": "2", "size": "9000000000", "download": "1"}}},      # other OS
    "102": {"config": {"language": "french"}, "manifests": {"public": {"gid": "3", "size": "700000000", "download": "1"}}},    # other language
    "103": {"dlcappid": "555", "manifests": {"public": {"gid": "4", "size": "800000000", "download": "1"}}},                    # DLC
    "104": {"config": {"language": "english"}, "manifests": {"public": {"gid": "5", "size": "300000000", "download": "100000000"}}},
    "105": {"manifests": {"beta": {"gid": "6", "size": "1"}}},                                                                  # no public manifest
    "106": {"config": {"oslist": "windows", "osarch": "32"}, "manifests": {"public": {"gid": "7", "size": "111", "download": "1"}}},
    "107": {"config": {"oslist": "windows", "osarch": "64"}, "manifests": {"public": {"gid": "8", "size": "222", "download": "1"}}},
    "branches": {"public": {"buildid": "1"}}, "baselanguages": "english"}}


class DepotMath(unittest.TestCase):
    def test_sums_only_what_a_normal_windows_install_downloads(self):
        r = compute_install_size(APP)
        self.assertEqual(r["size"], 5_000_000_000 + 300_000_000 + 222)          # not linux/french/DLC/beta/32-bit
        self.assertEqual((r["depots"], r["skipped_depots"], r["kind"]), (3, 1, "manifest"))
        self.assertEqual(r["download"], 2_000_000_000 + 100_000_000 + 1)
        self.assertEqual(r["original_year"], 2002)

    def test_old_record_shape_uses_maxsize(self):
        r = compute_install_size({"depots": {"10": {"manifests": {"public": "123"}, "maxsize": "4000"}}})
        self.assertEqual((r["size"], r["kind"]), (4000, "maxsize"))

    def test_32bit_only_is_used_when_no_64bit_exists(self):
        r = compute_install_size({"depots": {"1": {"config": {"osarch": "32"}, "manifests": {"public": {"size": "50", "download": "1"}}}}})
        self.assertEqual(r["size"], 50)

    def test_nothing_usable_means_none_never_zero(self):
        self.assertIsNone(compute_install_size({}))
        self.assertIsNone(compute_install_size({"depots": {"branches": {"public": {}}}}))
        self.assertIsNone(compute_install_size({"depots": {"5": {"depotfromapp": "1", "manifests": {}}}}))   # shared depot
        self.assertIsNone(compute_install_size({"depots": {"5": {"manifests": {"public": {"size": "0"}}}}}))


class FakeSteamClient:
    def __init__(self, login=1):
        self.login, self.calls = login, []
        self.apps = {1: APP, 2: {"depots": {"branches": {}}}, 3: {"_missing_token": True}}

    def anonymous_login(self):
        return self.login

    def get_product_info(self, apps=None, timeout=15):
        self.calls.append(list(apps))
        return {"apps": {a: self.apps[a] for a in apps if a in self.apps}}

    def logout(self):
        pass


class Worker(unittest.TestCase):
    def test_batches_and_reports_every_kind_of_outcome(self):
        out, client = [], FakeSteamClient()
        self.assertEqual(worker_run([1, 2, 3, 4], out.append, lambda: client, batch=2), 0)
        self.assertEqual(client.calls, [[1, 2], [3, 4]])                                   # batched
        status = {m["appid"]: m["status"] for m in out if "appid" in m}
        self.assertEqual(status, {1: "ok", 2: "no_depots", 3: "needs_token", 4: "unknown"})
        self.assertEqual(next(m for m in out if m.get("appid") == 1)["size"], 5_300_000_222)
        self.assertEqual([m for m in out if "progress" in m][-1], {"progress": 4, "total": 4})

    def test_login_failure_is_reported_not_swallowed(self):
        out = []
        self.assertEqual(worker_run([1], out.append, lambda: FakeSteamClient(login=5)), 1)
        self.assertIn("login failed", out[0]["error"])


def steam_db():
    conn = db.connect(":memory:")
    ids = {}
    ids["A"] = upsert_release(conn, source="gog", source_key="g1", name="Some Game", platform="pc",
                              size_bytes=4_500_000_000, size_conf="exact")               # GOG already knows the exact size
    ids["B"] = upsert_release(conn, source="gog", source_key="g2", name="Remake Game", platform="pc")
    ids["C"] = upsert_release(conn, source="gog", source_key="g3", name="Old Game", platform="pc")
    ids["D"] = upsert_release(conn, source="gog", source_key="g4", name="Nolink Game", platform="pc")
    ids["PS2"] = upsert_release(conn, source="redump", source_key="r1", name="Console Game", platform="ps2")
    claims.record_claim(conn, ids["A"], "orig_year", "igdb", 2003)
    claims.record_claim(conn, ids["B"], "orig_year", "igdb", 2020)
    for k, appid in (("A", "123"), ("B", "456"), ("C", "789")):
        conn.execute("INSERT INTO source_link(source,source_key,game_id,release_id) SELECT 'steam',?,game_id,id FROM releases WHERE id=?",
                     (appid, ids[k]))
    conn.commit()
    return conn, ids


RESULTS = [
    {"appid": 123, "status": "ok", "size": 5_000_000_000, "kind": "manifest", "depots": 2, "original_year": 2003},
    {"appid": 456, "status": "ok", "size": 1_000_000_000, "kind": "manifest", "depots": 1, "original_year": 2006},   # 2006 vs your 2020
    {"appid": 789, "status": "ok", "size": 2_000_000_000, "kind": "maxsize", "depots": 1, "original_year": None},
]


def runner_for(results, asked=None):
    def runner(appids, progress):
        if asked is not None:
            asked.extend(appids)
        yield from results
    return runner


class SteamPics(unittest.TestCase):
    def setUp(self):
        self.conn, self.ids = steam_db()

    def ctx(self, runner=None, http=None):
        c = ctx_for(self.conn, http or Fake([]), {})
        if runner:
            c.services["pics_runner"] = runner
        return c

    def size(self, k):
        return query.select(self.conn, [f"id={self.ids[k]}"])[0]

    def test_sizes_saved_as_depot_estimates_with_all_guards(self):
        asked = []
        ids = list(self.ids.values())
        out = service.refresh_fields(self.conn, self.ctx(runner_for(RESULTS, asked)), ["size"], ids, "steam_pics")
        self.assertEqual(sorted(asked), [123, 456, 789])                                  # only linked PC games are asked
        c = self.size("C")
        self.assertEqual((c["size_bytes"], c["size_conf"], c["size_source"]), (2_000_000_000, "depot", "steam_pics"))
        self.assertIsNone(self.size("B")["size_bytes"])                                   # year mismatch: a different game
        self.assertIsNone(self.size("D")["size_bytes"])                                   # never matched to Steam
        a = self.size("A")                                                                # GOG's exact size still wins
        self.assertEqual((a["size_bytes"], a["size_source"]), (4_500_000_000, "gog"))
        self.assertEqual(out["updated"], 2)                                               # A and C; B skipped by the guard
        self.assertEqual(query.count(self.conn, ["size_conf=depot", "size<3gb"]), 1)

    def test_optional_addon_missing_is_a_clear_message(self):
        orig = steam_pics.addon_installed
        steam_pics.addon_installed = lambda: False
        try:
            with self.assertRaises(LookupError) as cm:
                service.refresh_fields(self.conn, self.ctx(), ["size"], list(self.ids.values()), "steam_pics")
            self.assertIn("add-on", str(cm.exception))
            http = Fake([("https://api.gog.com/products", [])])                            # automatic mode: others still run
            out = service.refresh_fields(self.conn, self.ctx(http=http), ["size"], list(self.ids.values()))
            self.assertTrue(any("add-on" in n for n in out["notes"]))
        finally:
            steam_pics.addon_installed = orig

    def test_slow_sysreq_size_only_runs_when_chosen_explicitly(self):
        ids = list(self.ids.values())
        auto = service.estimate_refresh(self.conn, self.ctx(runner_for(RESULTS)), ["size"], ids)
        self.assertIn("steam_pics", auto["sources"])
        self.assertNotIn("steam", auto["sources"])                                        # the 125-hour method stays out of Automatic
        manual = service.estimate_refresh(self.conn, self.ctx(runner_for(RESULTS)), ["size"], ids, "steam")
        self.assertEqual(manual["sources"], ["steam"])
        self.assertIn("size", service.all_fields())

    def test_probe_reports_coverage_and_saves_nothing(self):
        results = RESULTS + [{"appid": 1, "status": "no_depots", "size": None}]
        self.conn.execute("INSERT INTO source_link(source,source_key,game_id,release_id) SELECT 'steam','1',game_id,id FROM releases WHERE id=?", (self.ids["D"],))
        rep = service.steam_probe(self.conn, self.ctx(runner_for(results)))
        self.assertEqual((rep["asked"], rep["with_size"], rep["manifest_sizes"], rep["maxsize_only"], rep["no_size"]), (4, 3, 2, 1, 1))
        self.assertEqual(rep["not_returned"], 0)
        self.assertEqual(rep["compared_count"], 1)                                        # only A has a GOG size to compare
        self.assertEqual(rep["compared_with_gog"][0]["ratio"], round(5_000_000_000 / 4_500_000_000, 2))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM claims WHERE source='steam_pics'").fetchone()[0], 0)

    def test_probe_needs_step_one_and_the_addon(self):
        conn = db.connect(":memory:")
        upsert_release(conn, source="gog", source_key="x", name="Solo", platform="pc")
        with self.assertRaises(LookupError) as cm:
            service.steam_probe(conn, ctx_for(conn, Fake([]), {}))
        self.assertTrue("add-on" in str(cm.exception) or "Match" in str(cm.exception))
        c = ctx_for(conn, Fake([]), {})
        c.services["pics_runner"] = runner_for([])
        with self.assertRaises(LookupError) as cm:
            service.steam_probe(conn, c)
        self.assertIn("step 1", str(cm.exception))

    def test_install_addon_reports_success_and_failure(self):
        ok = self.ctx()
        ok.services["pip"] = lambda: SimpleNamespace(returncode=0, stdout="", stderr="")
        self.assertEqual(service.install_steam_addon(ok), {"installed": True})
        bad = self.ctx()
        bad.services["pip"] = lambda: SimpleNamespace(returncode=1, stdout="", stderr="line1\nERROR: no internet")
        with self.assertRaises(RuntimeError) as cm:
            service.install_steam_addon(bad)
        self.assertIn("no internet", str(cm.exception))


def match_fixture():
    conn = db.connect(":memory:")
    ids = {}
    ids["hl"] = upsert_release(conn, source="igdb", source_key="pc:100", name="Half Life (renamed in my db)", platform="pc")
    ids["doom"] = upsert_release(conn, source="igdb", source_key="pc:101", name="Doom", platform="pc")
    ids["nogig"] = upsert_release(conn, source="gog", source_key="9", name="No IGDB Game", platform="pc")
    ids["done"] = upsert_release(conn, source="gog", source_key="8", name="Some Game", platform="pc")
    upsert_release(conn, source="redump", source_key="5", name="Half-Life", platform="ps2")           # consoles are never matched
    conn.execute("INSERT INTO source_link(source,source_key,game_id,release_id) SELECT 'steam','999',game_id,id FROM releases WHERE id=?", (ids["done"],))
    conn.commit()
    return conn, ids


def igdb_links_fake(seen):
    entries = [{"game": 100, "uid": "70", "url": "https://store.steampowered.com/app/70/Half-Life/"},
               {"game": 101, "uid": "2280", "url": "https://store.steampowered.com/app/2280/DOOM/"},
               {"game": 101, "uid": "379720", "url": "https://store.steampowered.com/app/379720/DOOM/"}]
    entries += [{"game": 50000 + i, "uid": str(i), "url": f"https://store.steampowered.com/app/{i}/x/"} for i in range(500)]   # forces a 2nd page

    def handler(method, url, body):
        text = (body or b"").decode()
        seen.append(text)
        off = int(text.split("offset ")[1].split(";")[0])
        return entries[off:off + 500]
    return Fake([("https://id.twitch.tv/oauth2/token", {"access_token": "tok", "expires_in": 5_000_000}),
                 ("https://api.igdb.com/v4/external_games", handler)])


IGDB_CFG = {"igdb": {"client_id": "cid", "client_secret": "sec"}}


class SteamMatch(unittest.TestCase):
    def test_igdb_ids_link_exactly_and_ambiguity_is_never_guessed(self):
        conn, ids = match_fixture()
        seen = []
        ctx = ctx_for(conn, igdb_links_fake(seen), IGDB_CFG)
        out = service.update_source(conn, ctx, "steam_match", {})
        self.assertEqual((out["linked_via_igdb"], out["linked_via_steam_list"], out["ambiguous_skipped"], out["already_linked"]), (1, 0, 1, 1))
        self.assertEqual(out["unmatched"], 1)                                              # No IGDB Game
        self.assertEqual(out["pc_games"], 4)
        self.assertEqual(out["methods"], ["IGDB (exact IDs)"])
        # linked by IGDB's ID even though the title in the database is different: that is the point
        self.assertEqual(conn.execute("SELECT source_key FROM source_link WHERE source='steam' AND release_id=?", (ids["hl"],)).fetchone()[0], "70")
        self.assertEqual(len(seen), 2)                                                     # paged: 503 entries = 2 requests
        self.assertTrue(all("store.steampowered.com/app/" in b and "category" not in b for b in seen))   # URL filter, not the retired field
        self.assertIsNotNone(ctx.state_get("steam", "matched_at"))
        again = service.steam_match(conn, ctx)
        self.assertEqual((again["newly_linked"], again["already_linked"]), (0, 2))
        # after matching, Steam refreshes no longer search by name for games that didn't match
        fake2 = steam_fake()
        service.refresh_fields(conn, ctx_for(conn, fake2, {}), ["price"], [ids["nogig"]], "steam")
        self.assertEqual(fake2.count("https://store.steampowered.com/api/storesearch"), 0)

    def test_optional_steam_key_links_the_rest_by_exact_title(self):
        conn, ids = match_fixture()
        apps = [{"appid": 900, "name": "No IGDB Game"}, {"appid": 901, "name": "Doom"}, {"appid": 902, "name": "DOOM"}]
        fake = igdb_links_fake([])
        fake.routes.append(("https://api.steampowered.com/IStoreService/GetAppList/v1/", {"response": {"apps": apps, "have_more_results": False}}))
        ctx = ctx_for(conn, fake, {**IGDB_CFG, "steam": {"api_key": "SECRETKEY123"}})
        out = service.steam_match(conn, ctx)
        self.assertEqual((out["linked_via_igdb"], out["linked_via_steam_list"]), (1, 1))
        self.assertEqual(out["ambiguous_skipped"], 1)                                      # Doom counted once, not once per method
        self.assertEqual(out["unmatched"], 0)
        self.assertEqual(out["methods"], ["IGDB (exact IDs)", "Steam game list (exact titles)"])
        self.assertEqual(conn.execute("SELECT source_key FROM source_link WHERE source='steam' AND release_id=?", (ids["nogig"],)).fetchone()[0], "900")

    def test_key_only_works_without_igdb(self):
        conn, ids = match_fixture()
        fake = Fake([("https://api.steampowered.com/IStoreService/GetAppList/v1/",
                      {"response": {"apps": [{"appid": 900, "name": "No IGDB Game"}], "have_more_results": False}})])
        out = service.steam_match(conn, ctx_for(conn, fake, {"steam": {"api_key": "k"}}))
        self.assertEqual((out["linked_via_igdb"], out["linked_via_steam_list"]), (0, 1))

    def test_clear_messages_when_nothing_can_run_or_a_service_refuses(self):
        conn, _ = match_fixture()
        with self.assertRaises(LookupError) as cm:
            service.steam_match(conn, ctx_for(conn, Fake([]), {}))
        self.assertIn("IGDB keys", str(cm.exception))
        refuse = Fake([("https://id.twitch.tv/oauth2/token", {"access_token": "t", "expires_in": 5_000_000}),
                       ("https://api.igdb.com/v4/external_games", (403, {}, b"nope"))])
        with self.assertRaises(LookupError) as cm:
            service.steam_match(conn, ctx_for(conn, refuse, IGDB_CFG))
        self.assertIn("Steam key", str(cm.exception))                                      # tells the user the fallback
        bad = Fake([("https://api.steampowered.com/IStoreService/GetAppList/v1/", (403, {}, b"Forbidden"))])
        with self.assertRaises(LookupError) as cm:
            service.steam_match(conn, ctx_for(conn, bad, {"steam": {"api_key": "wrong"}}))
        self.assertIn("rejected the API key", str(cm.exception))


class RealSubprocess(unittest.TestCase):
    """Runs the actual worker process. A stand-in `steam` package replaces Valve's client library."""

    def setUp(self):
        import os, tempfile
        self.tmp = Path(tempfile.mkdtemp())
        pkg = self.tmp / "steam"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "client.py").write_text(
            "APP = " + repr(APP) + "\n"
            "class SteamClient:\n"
            "    def anonymous_login(self): return 1\n"
            "    def get_product_info(self, apps=None, timeout=15):\n"
            "        return {'apps': {a: APP for a in apps if a < 1000}}\n"
            "    def logout(self): pass\n")
        self.saved = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(self.tmp)

    def tearDown(self):
        import os, shutil
        os.environ.pop("PYTHONPATH", None) if self.saved is None else os.environ.__setitem__("PYTHONPATH", self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_worker_process_end_to_end(self):
        msgs = []
        results = list(steam_pics._subprocess_runner([1, 2, 5000], lambda m, f=None: msgs.append(m)))
        by = {r["appid"]: r for r in results}
        self.assertEqual(by[1]["size"], 5_300_000_222)
        self.assertEqual(by[5000]["status"], "unknown")                                   # not returned by "Steam"
        self.assertTrue(any("3/3" in m for m in msgs))                                    # progress reached the parent
        self.assertFalse(steam_pics.addon_installed())                                    # this interpreter has no steam package...
        sys.path.insert(0, str(self.tmp))
        try:
            self.assertTrue(steam_pics.addon_installed())                                 # ...until one is on the path
        finally:
            sys.path.remove(str(self.tmp))
            for m in [m for m in sys.modules if m == "steam" or m.startswith("steam.")]:
                del sys.modules[m]

    def test_missing_addon_is_reported_from_the_worker(self):
        import os
        os.environ["PYTHONPATH"] = str(self.tmp / "nothing-here")                         # no steam package on the path
        with self.assertRaises(RuntimeError) as cm:
            list(steam_pics._subprocess_runner([1], lambda m, f=None: None))
        self.assertIn("not installed", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=1)
