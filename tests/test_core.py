import json
import os
import sys
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import adapters, claims, db, export, filters, pack, query, service
from gqe.adapters.redump import RedumpIngestor, parse_name
from gqe.ingest import import_csv_text
from gqe.units import format_size, parse_size

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_ps2.dat"


def fresh():
    conn = db.connect(":memory:")
    service.build_from_records(conn, RedumpIngestor(SAMPLE))
    return conn


def name_id(conn, name, region=None):
    q = "SELECT id FROM v_release WHERE name=?" + (" AND region=?" if region else "")
    return conn.execute(q, (name, region) if region else (name,)).fetchone()[0]


class Units(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_size("2gb"), 2 * 10**9)
        self.assertEqual(parse_size("1.5 GBs"), 1_500_000_000)
        self.assertEqual(parse_size("700mb"), 700_000_000)
        self.assertEqual(parse_size("1GiB"), 2**30)
        self.assertEqual(parse_size("2"), 2 * 10**9)
        with self.assertRaises(ValueError):
            parse_size("abc")

    def test_format(self):
        self.assertEqual(format_size(1_500_000_000), "1.50 GB")
        self.assertEqual(format_size(None), "?")


class Redump(unittest.TestCase):
    def test_name_parsing(self):
        p = parse_name("Legend of Zelda, The - Wind Waker (USA) (Disc 1)")
        self.assertEqual(p["base"], "The Legend of Zelda - Wind Waker")
        self.assertEqual(p["region"], "USA")
        self.assertEqual(parse_name("Ratchet & Clank (Europe, Australia)")["region"], "Europe, Australia")
        self.assertTrue(parse_name("Ico (USA) (Beta)")["variant"])

    def test_build(self):
        conn = fresh()
        rows = {r["name"] + "|" + (r["region"] or ""): r for r in query.select(conn)}
        self.assertNotIn("Ico|USA (Beta)", rows)
        zelda = rows["The Legend of Zelda - Wind Waker|USA"]
        self.assertEqual(zelda["size_bytes"], 2_000_000_000)   # two discs summed
        self.assertEqual(zelda["discs"], 2)
        self.assertEqual(zelda["size_conf"], "exact")
        self.assertEqual(len(query.select(conn)), 8)   # 10 entries - 1 beta - 1 (2 discs merged)

    def test_rebuild_is_idempotent(self):
        conn = fresh()
        before = query.count(conn)
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        self.assertEqual(query.count(conn), before)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM games").fetchone()[0], 7)  # regions share a game


class Filters(unittest.TestCase):
    def setUp(self):
        self.conn = fresh()

    def names(self, *f):
        return sorted({r["name"] for r in query.select(self.conn, list(f))})

    def test_size_ops_any_spelling(self):
        a = self.names("size<2gb")
        self.assertEqual(a, self.names("size lt 2gb"))
        self.assertEqual(a, self.names("size", "<", "2GBs"))
        self.assertEqual(a, ["Ico", "Katamari Damacy"])

    def test_range_and_platform_alias(self):
        self.assertEqual(self.names("size=2gb..4.3gb", 'console="playstation 2"'),
                         ["Okami", "Ratchet & Clank", "Shadow of the Colossus", "The Legend of Zelda - Wind Waker"])

    def test_text_and_region(self):
        self.assertEqual(self.names('name~"katamari"', "region=Europe"), ["Katamari Damacy"])
        self.assertEqual(len(query.select(self.conn, ["region~USA"])), 6)

    def test_unknown_handling(self):
        self.assertEqual(query.count(self.conn, ["year=2005"]), 0)          # unknown never matches
        self.assertEqual(query.count(self.conn, ["year=unknown"]), 8)
        cl = filters.parse_filters(["year>=2000"])
        cl = [filters.Clause(c.field, c.op, c.value, True) for c in cl]
        self.assertEqual(query.count(self.conn, cl), 8)                     # UI 'include unknown'

    def test_bad_input(self):
        with self.assertRaises(filters.FilterError):
            filters.parse_filters(["nonsense=1"])
        with self.assertRaises(filters.FilterError):
            query.select(self.conn, ["size~5"])

    def test_injection_is_inert(self):
        evil = filters.Clause("name", "~", "'; DROP TABLE games; --")
        self.assertEqual(query.count(self.conn, [evil]), 0)
        self.assertEqual(query.count(self.conn), 8)


class Claims(unittest.TestCase):
    def test_precedence_and_manual_override(self):
        conn = fresh()
        rid = name_id(conn, "Okami")
        claims.record_claim(conn, rid, "score", "mobygames", 80)
        claims.record_claim(conn, rid, "score", "igdb", 90)
        self.assertEqual(query.select(conn, [f"id={rid}"])[0]["score"], 90)     # igdb outranks mobygames
        claims.record_claim(conn, rid, "score", "manual", 55)
        r = query.select(conn, [f"id={rid}"])[0]
        self.assertEqual((r["score"], ), (55,))
        claims.record_claim(conn, rid, "size", "estimate", "1gb", "media_estimate")
        self.assertEqual(query.select(conn, [f"id={rid}"])[0]["size_bytes"], 4_200_000_000)  # exact redump wins

    def test_price_latest_wins_and_year_filter(self):
        conn = fresh()
        rid = name_id(conn, "Okami")
        claims.record_claim(conn, rid, "price", "a", 10, fetched_at="2026-01-01T00:00:00+00:00")
        claims.record_claim(conn, rid, "price", "b", 12, fetched_at="2026-06-01T00:00:00+00:00")
        self.assertEqual(query.select(conn, [f"id={rid}"])[0]["price"], 12.0)
        claims.record_claim(conn, rid, "orig_year", "igdb", 2006)
        claims.record_claim(conn, rid, "genres", "igdb", "Action, Adventure")
        self.assertEqual(query.count(conn, ["year=2006", "genre:adventure", "size<5gb"]), 1)

    def test_csv_import(self):
        conn = fresh()
        rid = name_id(conn, "Bully")
        out = import_csv_text(conn, f"id,price,score\n{rid},9.99,88%\n99999,1,1\n")
        self.assertEqual(out["applied"], 2)
        self.assertEqual(out["skipped"], 2)
        r = query.select(conn, [f"id={rid}"])[0]
        self.assertEqual((r["price"], r["score"]), (9.99, 88.0))


class Packing(unittest.TestCase):
    def rows(self):
        return [{"id": i, "game_id": i, "name": f"g{i}", "size_bytes": s, "score": sc}
                for i, (s, sc) in enumerate([(4_000_000_000, 95), (2_000_000_000, 80), (2_000_000_000, 70),
                                              (1_000_000_000, 10), (3_000_000_000, 90)], 1)]

    def test_count_and_score(self):
        r = pack.pack(self.rows(), [5_000_000_000], rank="count")
        self.assertEqual(r["total_items"], 3)   # 1+2+2 GB beats any 2-game combo
        r = pack.pack(self.rows(), [5_000_000_000], rank="score")
        self.assertEqual({i["name"] for i in r["bins"][0].items}, {"g2", "g5"})  # 2+3 GB, best value
        self.assertLessEqual(r["bins"][0].used, 5_000_000_000)

    def test_multi_bin_never_overflows_and_no_repeats(self):
        r = pack.pack(self.rows(), pack.parse_bins("2x4.7gb"), rank="score")
        ids = [i["id"] for b in r["bins"] for i in b.items]
        self.assertEqual(len(ids), len(set(ids)))
        for b in r["bins"]:
            self.assertLessEqual(b.used, b.capacity)

    def test_max_items_and_dedupe(self):
        r = pack.pack(self.rows(), [20_000_000_000], rank="score", max_items=2)
        self.assertEqual(r["total_items"], 2)
        rows = self.rows() + [{"id": 9, "game_id": 1, "name": "g1 alt", "size_bytes": 1_000_000_000, "score": 95}]
        r = pack.pack(rows, [20_000_000_000], rank="count")
        self.assertEqual(sum(1 for b in r["bins"] for i in b.items if i["game_id"] == 1), 1)

    def test_bins_spec(self):
        self.assertEqual(pack.parse_bins("10x4.7gb"), [4_700_000_000] * 10)
        self.assertEqual(pack.parse_bins("120gb"), [120_000_000_000])


class Export(unittest.TestCase):
    def test_formats_and_detail(self):
        conn = fresh()
        rows = query.select(conn, ["size<2gb"], "size-")
        csv_text = export.render(rows, "csv", "minimal")
        self.assertTrue(csv_text.startswith("name,platform,year,size_gb"))
        self.assertEqual(len(json.loads(export.render(rows, "json", "full"))), len(rows))
        self.assertIn("| Name |", export.render(rows, "md", "standard"))
        self.assertIn("Size (GB)", export.render(rows, "txt", "standard"))
        with self.assertRaises(ValueError):
            export.render(rows, "csv", columns=["nope"])


class Service(unittest.TestCase):
    def test_refresh_without_sources_is_a_clear_error(self):
        conn = fresh()
        with self.assertRaises(LookupError) as cm:
            service.refresh_field(conn, "price", [1])          # release 1 is a PS2 game; Steam/GOG are PC-only
        self.assertIn("PC-only", str(cm.exception))
        with self.assertRaises(LookupError) as cm:
            service.refresh_field(conn, "category", [1], source="nope")
        self.assertIn("Planned", str(cm.exception))

    def test_refresh_with_a_fake_adapter(self):
        conn = fresh()

        class Fake(adapters.Refresher):
            name, fields = "fake", frozenset({"price"})

            def fetch(self, rows, fields, links):
                for r in rows:
                    yield adapters.Update(r["id"], "price", 4.5)

        adapters.REFRESHERS["fake"] = Fake
        try:
            ids = [r["id"] for r in query.select(conn, ["size<2gb"])]
            out = service.refresh_field(conn, "price", ids)
            self.assertEqual(out["updated"], len(ids))
            self.assertEqual(query.count(conn, ["price=4.5"]), len(ids))
            self.assertEqual(query.count(conn, ["price=unknown"]), query.count(conn) - len(ids))
        finally:
            adapters.REFRESHERS.pop("fake")

    def test_sets_and_flags_and_facets(self):
        conn = fresh()
        ids = [r["id"] for r in query.select(conn, ["size<2gb"])]
        service.save_set(conn, "small", ids)
        self.assertEqual(query.count(conn, ["set=small"]), len(ids))
        gid = query.select(conn, [f"id={ids[0]}"])[0]["game_id"]
        service.set_user_flag(conn, gid, "favorite", True)
        self.assertGreaterEqual(query.count(conn, ["fav=yes"]), 1)
        f = service.facets(conn)
        self.assertEqual(f["platforms"][0]["value"], "ps2")
        self.assertIn("USA", [r["value"] for r in f["regions"]])


class HttpApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        from http.server import ThreadingHTTPServer
        from gqe.server import Jobs, make_handler
        cls.home = Path(tempfile.mkdtemp())
        os.environ["GQE_HOME"] = str(cls.home)             # config.json goes here, not into ~
        cls.path = cls.home / "t.db"
        db.connect(cls.path).close()
        ref = {}
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.path, Jobs(cls.path), ref, 0))
        ref["server"] = cls.srv
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        os.environ.pop("GQE_HOME", None)

    def call(self, path, body=None, raw=False, headers=None):
        h = {"X-GQE": "1", **(headers or {})}
        data = body if raw else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, headers=h, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_full_flow(self):
        import time
        code, html = self.call("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Game Query Engine", html)
        code, out = self.call("/api/upload_dat?name=sample.dat", SAMPLE.read_bytes(), raw=True)
        job = json.loads(out)["job"]
        for _ in range(50):
            j = json.loads(self.call(f"/api/job?id={job}")[1])
            if j["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(j["state"], "done", j)
        code, out = self.call("/api/query", {"filters": [{"field": "size", "op": "<=", "value": "2GB"}],
                                             "sort": "size-", "page_size": 3})
        q = json.loads(out)
        self.assertEqual(q["total"], 4)
        self.assertEqual(len(q["rows"]), 3)
        code, out = self.call("/api/pack", {"filters": [], "bins": "8gb", "rank": "count"})
        self.assertLessEqual(json.loads(out)["bins"][0]["used"], 8_000_000_000)
        code, out = self.call("/api/export", {"filters": [], "format": "csv", "detail": "minimal"})
        self.assertTrue(out.startswith(b"\xef\xbb\xbfname,platform"))
        # Refresh runs as a job; PS2 games have no covering source, and the user is told why
        code, out = self.call("/api/refresh", {"field": "price", "filters": []})
        self.assertEqual(code, 200)
        job = json.loads(out)["job"]
        for _ in range(50):
            j = json.loads(self.call(f"/api/job?id={job}")[1])
            if j["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(j["state"], "error")
        self.assertIn("PC-only", j["message"])
        code, out = self.call("/api/refresh_estimate", {"field": "price", "filters": []})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(out)["needs"])
        code, out = self.call("/api/refresh", {"field": "bogus", "filters": []})
        self.assertEqual(code, 400)
        # keys are stored server-side and never sent back
        code, out = self.call("/api/config", {"igdb": {"client_id": "abc", "client_secret": "topsecret"}})
        self.assertEqual(json.loads(out), {"igdb": {"client_id": "abc", "client_secret_set": True}, "steam": {"api_key_set": False}, "rawg": {"api_key_set": False}})
        code, out = self.call("/api/config", {"steam": {"api_key": "STEAMSECRET"}})
        self.assertEqual(json.loads(out)["steam"], {"api_key_set": True})
        self.assertNotIn(b"topsecret", self.call("/api/config")[1])
        self.assertNotIn(b"STEAMSECRET", self.call("/api/config")[1])
        self.assertTrue(next(r for r in json.loads(self.call("/api/adapters")[1])["refreshers"] if r["name"] == "igdb")["ready"])
        info = json.loads(self.call("/api/info")[1])
        self.assertEqual(info["name"], "Game Query Engine")
        code, out = self.call("/api/update_source", {"source": "nonsense"})
        job = json.loads(out)["job"]
        for _ in range(50):
            j = json.loads(self.call(f"/api/job?id={job}")[1])
            if j["state"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(j["state"], "error")
        code, out = self.call("/api/query", {"filters": [{"field": "bogus", "op": "=", "value": "1"}]})
        self.assertEqual(code, 400)

    def test_csrf_guards(self):
        req = urllib.request.Request(self.base + "/api/query", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 403)              # no X-GQE header
        req = urllib.request.Request(self.base + "/api/facets", headers={"Host": "evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 403)              # DNS-rebinding style host


if __name__ == "__main__":
    unittest.main(verbosity=1)
