import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import adapters, claims, config, db, paths, query, service
from gqe.adapters import Context, gog, igdb, steam
from gqe.adapters.redump import RedumpIngestor
from gqe.http import Http, HttpError
from gqe.ingest import upsert_release

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sample_ps2.dat"
NOSLEEP = lambda s: None  # noqa: E731


class Fake:
    """Stands in for the internet. routes: [(url_prefix, dict|list|callable|(status,hdrs,bytes))]"""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        for prefix, r in self.routes:
            if url.startswith(prefix):
                r = r(method, url, body) if callable(r) else r
                return r if isinstance(r, tuple) else (200, {}, json.dumps(r).encode())
        return 404, {}, b"{}"

    def count(self, prefix):
        return sum(1 for _, u, _ in self.calls if u.startswith(prefix))


def ctx_for(conn, fake, cfg=None):
    return Context(conn, Http(transport=fake, sleep=NOSLEEP), cfg or {})


def ts(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


# ------------------------------------------------------------------ http
class HttpLayer(unittest.TestCase):
    def test_retry_after_and_cache_and_errors(self):
        sleeps, n = [], {"c": 0}

        def flaky(m, u, b):
            n["c"] += 1
            return (429, {"retry-after": "7"}, b"slow down") if n["c"] == 1 else {"ok": True}

        tmp = tempfile.mkdtemp()
        http = Http(cache_dir=tmp, transport=Fake([("https://x.test/", flaky)]), sleep=sleeps.append)
        self.assertEqual(http.get_json("https://x.test/a", ttl=60), {"ok": True})
        self.assertIn(7.0, sleeps)                                   # honoured Retry-After
        http.get_json("https://x.test/a", ttl=60)
        self.assertEqual(n["c"], 2)                                  # second call served from cache
        with self.assertRaises(HttpError) as cm:
            Http(transport=Fake([]), sleep=NOSLEEP).get_json("https://x.test/missing")
        self.assertEqual(cm.exception.status, 404)
        shutil.rmtree(tmp)


# ------------------------------------------------------------------ GOG
def gog_product(pid, title, ptype="game", rel="1998.07.31", store="2008.07.14", price="14.99",
                genres=("Strategy", "Turn-based"), rating=43, count=778):
    return {"id": str(pid), "title": title, "productType": ptype, "releaseDate": rel, "storeReleaseDate": store,
            "price": {"finalMoney": {"amount": price, "currency": "USD"}},
            "genres": [{"name": g} for g in genres], "reviewsRating": rating, "reviewsCount": count}


class Gog(unittest.TestCase):
    def catalog(self, monkey_page=2):
        pages = {"0": [gog_product(10, "M.A.X. Mech Assault™"), gog_product(20, "Old Game", rel="1993.01.01", price="5.99")],
                 "20": [gog_product(30, "Role Game", genres=("Role-playing",), rel="2010.05.05", rating=0, count=0)]}

        def handler(m, url, body):
            after = re.search(r"searchAfter=(\d+)", url).group(1)
            return {"pages": 2, "productCount": 3, "products": pages.get(after, [])}
        gog.PAGE = monkey_page
        return Fake([("https://catalog.gog.com/v1/catalog", handler)])

    def tearDown(self):
        gog.PAGE = 48

    def test_catalog_import_paginates_and_uses_original_dates(self):
        conn = db.connect(":memory:")
        fake = self.catalog()
        r = service.build_from_records(conn, gog.GogIngestor(ctx_for(conn, fake)))
        self.assertEqual(r["records"], 3)
        self.assertEqual(fake.count("https://catalog.gog.com"), 2)             # two pages, then stop
        rows = {x["name"]: x for x in query.select(conn)}
        m = rows["M.A.X. Mech Assault"]                                        # trademark sign stripped
        self.assertEqual(m["release_date"], "1998-07-31")                      # ORIGINAL date, not the 2008 store date
        self.assertEqual((m["orig_year"], m["drm"], m["price"], m["score"]), (1998, "drm-free", 14.99, 86.0))
        self.assertEqual(m["genres"], "|strategy|turn-based|")
        self.assertIsNone(rows["Role Game"]["score"])                          # no reviews -> no score
        self.assertEqual(rows["Role Game"]["genres"], "|rpg|")
        self.assertEqual(query.count(conn, ["year=1990..1999", "drm=drm-free", "price<=10"]), 1)
        service.build_from_records(conn, gog.GogIngestor(ctx_for(conn, self.catalog())))
        self.assertEqual(query.count(conn), 3)                                 # re-import is idempotent

    def test_installer_sizes_prefer_windows_english(self):
        conn = db.connect(":memory:")
        service.build_from_records(conn, gog.GogIngestor(ctx_for(conn, self.catalog())))
        ids = {r["name"]: r["id"] for r in query.select(conn)}
        installers = {"installers": [
            {"os": "linux", "language": "en", "total_size": 999},
            {"os": "windows", "language": "de", "total_size": 5},
            {"os": "windows", "language": "en", "total_size": 1_500_000_000}]}
        fake = Fake([("https://api.gog.com/products", [{"id": 10, "downloads": installers}, {"id": 20, "downloads": {}}])])
        out = service.refresh_fields(conn, ctx_for(conn, fake), ["size"], list(ids.values()), "gog")
        self.assertEqual(out["updated"], 1)                                    # game 20 has no installer data: skipped
        r = query.select(conn, [f"id={ids['M.A.X. Mech Assault']}"])[0]
        self.assertEqual((r["size_bytes"], r["size_conf"], r["size_source"]), (1_500_000_000, "exact", "gog"))
        self.assertEqual(query.count(conn, ["size<2gb", "size_conf=exact"]), 1)

    def test_update_source_gog_sizes_only_targets_missing(self):
        conn = db.connect(":memory:")
        service.build_from_records(conn, gog.GogIngestor(ctx_for(conn, self.catalog())))
        fake = Fake([("https://api.gog.com/products", [])])
        service.update_source(conn, ctx_for(conn, fake), "gog_sizes", {})
        self.assertEqual(fake.count("https://api.gog.com"), 1)                 # one batched request for all 3


# ------------------------------------------------------------------ IGDB
def igdb_fake(platform_name="PlayStation 2", games=None):
    games = games if games is not None else [
        {"id": 1, "name": "Katamari Damacy", "first_release_date": ts(2004, 3, 18), "total_rating": 82.44,
         "total_rating_count": 300, "genres": [{"name": "Puzzle"}, {"name": "Role-playing (RPG)"}],
         "release_dates": [{"platform": 8, "date": ts(2004, 3, 18)}, {"platform": 8, "date": ts(2004, 9, 24)},
                           {"platform": 6, "date": ts(2009, 1, 1)}]},
        {"id": 2, "name": "Ratchet and Clank", "first_release_date": ts(2002, 11, 4), "total_rating": 79.0,
         "total_rating_count": 250, "genres": [{"name": "Platform"}, {"name": "Shooter"}],
         "release_dates": [{"platform": 8, "date": ts(2002, 11, 4)}]}]
    return Fake([
        ("https://id.twitch.tv/oauth2/token", {"access_token": "tok", "expires_in": 5_000_000}),
        ("https://api.igdb.com/v4/platforms", [{"id": 8, "name": platform_name}]),
        ("https://api.igdb.com/v4/games", lambda m, u, b: games if b and b"platforms = (8)" in b else []),
    ])


CFG = {"igdb": {"client_id": "cid", "client_secret": "sec"}}


class Igdb(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        service.build_from_records(self.conn, RedumpIngestor(SAMPLE))

    def test_needs_keys(self):
        ids = [r["id"] for r in query.select(self.conn)]
        with self.assertRaises(LookupError) as cm:
            service.refresh_fields(self.conn, ctx_for(self.conn, igdb_fake()), ["score"], ids, "igdb")
        self.assertIn("needs setup", str(cm.exception))

    def test_enriches_years_genres_scores_per_platform(self):
        fake = igdb_fake()
        ids = [r["id"] for r in query.select(self.conn)]
        ctx = ctx_for(self.conn, fake, CFG)
        out = service.refresh_fields(self.conn, ctx, ["orig_year", "release_date", "genres", "score"], ids, "igdb")
        self.assertGreater(out["updated"], 0)
        k = query.select(self.conn, ["name~katamari"])
        self.assertEqual(len(k), 2)                                             # USA + Europe releases both enriched
        self.assertTrue(all(r["release_date"] == "2004-03-18" for r in k))      # earliest date ON THIS PLATFORM
        self.assertTrue(all(r["orig_year"] == 2004 and r["score"] == 82.4 for r in k))
        self.assertEqual(k[0]["genres"], "|puzzle|rpg|")
        # 'Ratchet & Clank' matches IGDB's 'Ratchet and Clank' (ampersand normalisation)
        self.assertEqual(query.count(self.conn, ["year=2002", "genre:platformer", "score>=60"]), 1)
        # the user's headline use case: PS2, 2004, under 2 GB, score >= 60
        self.assertEqual(query.count(self.conn, ["platform=ps2", "year=2004", "size<2gb", "score>=60%"]), 2)
        # bulk download happened once per platform, token fetched once and persisted for next time
        self.assertEqual(fake.count("https://id.twitch.tv"), 1)
        service.refresh_fields(self.conn, ctx_for(self.conn, fake, CFG), ["score"], ids, "igdb")
        self.assertEqual(fake.count("https://id.twitch.tv"), 1)

    def test_platform_id_mismatch_fails_loudly(self):
        ids = [r["id"] for r in query.select(self.conn)]
        with self.assertRaises(LookupError) as cm:
            service.refresh_fields(self.conn, ctx_for(self.conn, igdb_fake("Nintendo 64"), CFG), ["score"], ids, "igdb")
        self.assertIn("platform id 8", str(cm.exception))
        self.assertEqual(query.count(self.conn, ["score!=unknown"]), 0)         # nothing mislabelled

    def test_manual_edit_beats_igdb(self):
        ids = [r["id"] for r in query.select(self.conn)]
        rid = query.select(self.conn, ["name~katamari", "region=USA"])[0]["id"]
        claims.record_claim(self.conn, rid, "score", "manual", 99)
        service.refresh_fields(self.conn, ctx_for(self.conn, igdb_fake(), CFG), ["score"], ids, "igdb")
        self.assertEqual(query.select(self.conn, [f"id={rid}"])[0]["score"], 99.0)


# ------------------------------------------------------------------ Steam
STEAM_DETAILS = {"123": {"success": True, "data": {
    "is_free": False, "price_overview": {"currency": "USD", "initial": 1999, "final": 999},
    "metacritic": {"score": 88}, "genres": [{"id": "1", "description": "Action"}, {"id": "3", "description": "Role-Playing"}],
    "release_date": {"coming_soon": False, "date": "12 May, 2011"},
    "pc_requirements": {"minimum": "<strong>Minimum:</strong><br><ul class=\"bb_ul\"><li><strong>Storage:</strong> 2 GB available space</li></ul>"}}}}


def steam_fake():
    def details(m, url, body):
        if "filters=price_overview" in url:
            return {"123": {"success": True, "data": {"price_overview": STEAM_DETAILS["123"]["data"]["price_overview"]}},
                    "456": {"success": True, "data": []}}                        # free/unpriced -> empty list
        return STEAM_DETAILS
    return Fake([("https://store.steampowered.com/api/storesearch/",
                  lambda m, u, b: {"total": 1, "items": [{"id": 123, "name": "Some Game"}]} if "Some+Game" in u
                  else {"total": 0, "items": []}),
                 ("https://store.steampowered.com/api/appdetails", details)])


class Steam(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.rid = upsert_release(self.conn, source="gog", source_key="1", name="Some Game", platform="pc",
                                  size_bytes=1_000_000_000, size_conf="exact")
        self.other = upsert_release(self.conn, source="gog", source_key="2", name="Unknown Thing", platform="pc")
        self.ps2 = upsert_release(self.conn, source="redump", source_key="x", name="Console Game", platform="ps2")

    def test_parsers(self):
        self.assertEqual(steam.parse_release_date("12 May, 2011"), "2011-05-12")
        self.assertEqual(steam.parse_release_date("May 12, 2011"), "2011-05-12")
        self.assertEqual(steam.parse_release_date("May 2011"), "2011-05")
        self.assertEqual(steam.parse_release_date("Q3 2003"), "2003")
        self.assertIsNone(steam.parse_release_date("Coming soon"))
        self.assertEqual(steam.sysreq_size({"minimum": "<li>Hard Drive: 500 MB</li>"}), 500_000_000)
        self.assertEqual(steam.sysreq_size({"minimum": "<li>8 GB available space</li>"}), 8_000_000_000)
        self.assertIsNone(steam.sysreq_size([]))

    def test_price_only_uses_one_batched_call_and_saves_link(self):
        fake = steam_fake()
        out = service.refresh_fields(self.conn, ctx_for(self.conn, fake), ["price"], [self.rid, self.other, self.ps2])
        self.assertEqual(out["updated"], 1)
        self.assertEqual(query.select(self.conn, [f"id={self.rid}"])[0]["price"], 9.99)
        self.assertEqual(fake.count("https://store.steampowered.com/api/appdetails"), 1)
        self.assertEqual(self.conn.execute("SELECT source_key FROM source_link WHERE source='steam'").fetchone()[0], "123")
        n = fake.count("https://store.steampowered.com/api/storesearch")
        service.refresh_fields(self.conn, ctx_for(self.conn, fake), ["price"], [self.rid])     # link reused: no new search
        self.assertEqual(fake.count("https://store.steampowered.com/api/storesearch"), n)

    def test_full_details_and_precedence(self):
        out = service.refresh_fields(self.conn, ctx_for(self.conn, steam_fake()),
                                     ["score", "genres", "release_date", "size", "drm"], [self.rid], "steam")
        r = query.select(self.conn, [f"id={self.rid}"])[0]
        self.assertEqual((r["score"], r["release_date"], r["drm"]), (88.0, "2011-05-12", "steam"))
        self.assertEqual(r["genres"], "|action|rpg|")
        self.assertEqual((r["size_bytes"], r["size_conf"]), (1_000_000_000, "exact"))   # GOG-style exact beats sysreq estimate
        claim = self.conn.execute("SELECT value,confidence FROM claims WHERE field='size' AND source='steam'").fetchone()
        self.assertEqual((claim[0], claim[1]), ("2000000000", "sysreq_estimate"))       # ...but the estimate is still stored

    def test_estimate_and_console_rows(self):
        est = service.estimate_refresh(self.conn, ctx_for(self.conn, steam_fake()), ["price"], [self.rid, self.other])
        self.assertEqual(est["sources"], ["steam"])
        self.assertGreater(est["seconds"], 0)
        with self.assertRaises(LookupError) as cm:
            service.refresh_fields(self.conn, ctx_for(self.conn, steam_fake()), ["price"], [self.ps2])
        self.assertIn("PC-only", str(cm.exception))


# ------------------------------------------------------------------ safe updates
class SafeUpdates(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_v1(self, path):
        c = sqlite3.connect(path)
        c.executescript(db.TABLES)
        c.execute("INSERT INTO meta VALUES('schema_version','1')")
        c.execute("INSERT INTO games(id,name,name_norm) VALUES(1,'Okami','okami')")
        c.execute("INSERT INTO releases(id,game_id,platform,size_bytes) VALUES(1,1,'ps2',4200000000)")
        c.commit()
        c.close()

    def test_old_database_is_upgraded_in_place_with_backup(self):
        path = self.dir / "old.db"
        self.make_v1(path)
        conn = db.connect(path)
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
        self.assertEqual(query.select(conn)[0]["size_bytes"], 4_200_000_000)             # data intact
        conn.execute("SELECT * FROM source_state")                                       # new table exists
        backups = list((self.dir / "backups").glob("old-v1-*.db"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(sqlite3.connect(backups[0]).execute("SELECT COUNT(*) FROM games").fetchone()[0], 1)

    def test_newer_database_is_refused_untouched(self):
        path = self.dir / "new.db"
        self.make_v1(path)
        c = sqlite3.connect(path)
        c.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        c.commit()
        c.close()
        with self.assertRaises(db.DatabaseTooNew):
            db.connect(path)
        self.assertEqual(sqlite3.connect(path).execute("SELECT COUNT(*) FROM games").fetchone()[0], 1)

    def test_changed_ranking_rules_re_resolve_from_stored_claims(self):
        path = self.dir / "r.db"
        conn = db.connect(path)
        rid = upsert_release(conn, source="redump", source_key="a", name="Okami", platform="ps2")
        claims.record_claim(conn, rid, "score", "igdb", 90)
        claims.record_claim(conn, rid, "score", "steam", 70)
        conn.commit()
        self.assertEqual(query.select(conn)[0]["score"], 90.0)
        conn.close()
        old = list(claims.PRECEDENCE["score"])
        claims.PRECEDENCE["score"] = ["steam", "igdb"]                                   # simulates a git pull changing rules
        try:
            conn = db.connect(path)
            self.assertEqual(query.select(conn)[0]["score"], 70.0)
        finally:
            claims.PRECEDENCE["score"] = old

    def test_user_data_lives_outside_the_repo_and_config_is_private(self):
        os.environ.pop("GQE_HOME", None)
        os.environ.pop("GQE_DB", None)
        self.assertEqual(paths.db_path(), ROOT / "data" / "gqe.db")                 # project folder by default
        self.assertIn("data/", (ROOT / ".gitignore").read_text())                       # ...and git never sees it
        os.environ["GQE_HOME"] = str(self.dir)
        try:
            config.update({"igdb": {"client_id": " abc ", "client_secret": "s3cret"}})
            cfg = config.load()
            self.assertEqual(cfg["igdb"]["client_id"], "abc")
            self.assertEqual(config.public_view(cfg), {"igdb": {"client_id": "abc", "client_secret_set": True}, "steam": {"api_key_set": False}, "rawg": {"api_key_set": False}})
            self.assertNotIn("s3cret", json.dumps(config.public_view(cfg)))
            config.update({"igdb": {"client_secret": ""}})                              # clearing works
            self.assertFalse(config.public_view(config.load())["igdb"]["client_secret_set"])
            if os.name == "posix":
                self.assertEqual(oct((self.dir / "config.json").stat().st_mode & 0o777), "0o600")
        finally:
            del os.environ["GQE_HOME"]

    def test_gitignore_protects_user_files(self):
        text = (ROOT / ".gitignore").read_text()
        for pat in ("*.db", "config.json", "cache/", "backups/", "data/"):
            self.assertIn(pat, text)


class UiScript(unittest.TestCase):
    def test_javascript_parses(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        js = re.search(r"<script>(.*)</script>", (ROOT / "gqe/static/index.html").read_text(), re.S).group(1)
        f = Path(tempfile.mkdtemp()) / "ui.js"
        f.write_text(js)
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
