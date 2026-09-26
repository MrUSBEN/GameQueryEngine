import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import backup, db, paths, query, service
from gqe.adapters import Context
from gqe.adapters.redump import RedumpIngestor
from gqe.adapters import wikidata
from gqe.http import Http

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sample_ps2.dat"


def populated(path):
    conn = db.connect(path)
    service.build_from_records(conn, RedumpIngestor(SAMPLE))
    conn.close()


class DataFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE", "GQE_HOME", "GQE_DB")}
        os.environ.pop("GQE_DB", None)
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.tmp / "home")
        os.environ["GQE_HOME"] = str(self.tmp / "project" / "data")

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_existing_home_data_is_copied_not_moved(self):
        old = self.tmp / "home" / ".gamedex"                                              # the pre-rename location
        old.mkdir(parents=True)
        populated(old / "gamedex.db")
        (old / "config.json").write_text('{"igdb": {"client_id": "abc"}}')
        notice = paths.prepare()
        self.assertIn("Copied", notice)
        new = self.tmp / "project" / "data"
        conn = db.connect(new / "gqe.db")                                                 # arrives under the NEW name
        self.assertEqual(query.count(conn), 8)                                            # data arrived intact
        self.assertTrue((new / "config.json").exists())
        self.assertTrue((old / "gamedex.db").exists())                                    # original untouched
        self.assertTrue((new / "README.txt").exists())
        self.assertIsNone(paths.prepare())                                                # only ever adopts once

    def test_fresh_install_and_rename(self):
        self.assertIsNone(paths.prepare())
        data = self.tmp / "project" / "data"
        self.assertTrue(data.is_dir())
        (data / "oldname.db").write_bytes(b"x")
        (data / "oldname.db-wal").write_bytes(b"w")
        saved = paths.LEGACY_IDS
        paths.LEGACY_IDS = ("oldname",)
        try:
            paths.prepare()
        finally:
            paths.LEGACY_IDS = saved
        self.assertTrue((data / "gqe.db").exists())                                       # db follows an app rename
        self.assertTrue((data / "gqe.db-wal").exists())                                   # ...with its write-ahead file
        self.assertFalse((data / "oldname.db").exists())

    def test_upgrade_from_the_previous_release_layout(self):
        """0.3 users have <project>/data/gamedex.db: it must become gqe.db with all data intact."""
        data = self.tmp / "project" / "data"
        data.mkdir(parents=True)
        populated(data / "gamedex.db")
        (data / "config.json").write_text('{"igdb": {"client_id": "abc"}}')
        self.assertIsNone(paths.prepare())
        self.assertFalse((data / "gamedex.db").exists())
        conn = db.connect(data / "gqe.db")
        self.assertEqual(query.count(conn), 8)
        self.assertEqual((data / "config.json").read_text(), '{"igdb": {"client_id": "abc"}}')

    def test_old_environment_variable_names_still_work(self):
        os.environ.pop("GQE_HOME", None)
        os.environ["GAMEDEX_HOME"] = str(self.tmp / "legacyenv")
        try:
            self.assertEqual(paths.app_dir(), self.tmp / "legacyenv")
        finally:
            os.environ.pop("GAMEDEX_HOME", None)


class BackupRestore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "gqe.db"
        populated(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def counts(self):
        c = db.connect(self.path)
        try:
            return query.count(c)
        finally:
            c.close()

    def test_zip_roundtrip_with_safety_copy(self):
        conn = db.connect(self.path)
        data = backup.make_zip(conn)
        conn.close()
        z = zipfile.ZipFile(io.BytesIO(data))
        manifest = json.loads(z.read("manifest.json"))
        self.assertEqual((manifest["releases"], manifest["schema_version"]), (8, db.SCHEMA_VERSION))
        conn = db.connect(self.path)
        conn.execute("DELETE FROM releases WHERE id > 2")
        conn.commit()
        conn.close()
        self.assertEqual(self.counts(), 2)
        out = backup.restore(self.path, data)
        self.assertEqual(out["releases"], 8)
        self.assertEqual(self.counts(), 8)
        self.assertTrue(out["safety_copy"])                                               # what was replaced is kept
        self.assertTrue((self.tmp / "backups" / out["safety_copy"]).exists())

    def test_raw_db_and_older_schema_are_accepted_and_upgraded(self):
        old = self.tmp / "v1.db"
        c = sqlite3.connect(old)
        c.executescript(db.TABLES)
        c.execute("INSERT INTO meta VALUES('schema_version','1')")
        c.execute("INSERT INTO games(id,name,name_norm) VALUES(1,'Okami','okami')")
        c.execute("INSERT INTO releases(id,game_id,platform) VALUES(1,1,'ps2')")
        c.commit()
        c.close()
        out = backup.restore(self.path, old.read_bytes())
        self.assertEqual((out["releases"], out["backup_version"]), (1, 1))
        conn = db.connect(self.path)
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(db.SCHEMA_VERSION))
        conn.execute("SELECT * FROM source_state")
        conn.close()

    def test_bad_files_are_rejected_and_nothing_changes(self):
        newer = self.tmp / "newer.db"
        c = sqlite3.connect(newer)
        c.executescript(db.TABLES)
        c.execute("INSERT INTO meta VALUES('schema_version','99')")
        c.commit()
        c.close()
        garbage_zip = io.BytesIO()
        with zipfile.ZipFile(garbage_zip, "w") as z:
            z.writestr("notes.txt", "hi")
        for name, blob, msg in (("garbage", b"not a backup at all", "look like a backup"),
                                ("newer", newer.read_bytes(), "newer version"),
                                ("empty zip", garbage_zip.getvalue(), "doesn't contain a database"),
                                ("truncated", b"PK\x03\x04broken", "not a valid zip")):
            with self.assertRaises(backup.BackupError, msg=name) as cm:
                backup.restore(self.path, blob)
            self.assertIn(msg, str(cm.exception))
        self.assertEqual(self.counts(), 8)


class BackupApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from gqe.server import Jobs, make_handler
        cls.home = Path(tempfile.mkdtemp())
        os.environ["GQE_HOME"] = str(cls.home)
        cls.path = cls.home / "gqe.db"
        populated(cls.path)
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

    def post(self, path, body=b"{}"):
        req = urllib.request.Request(self.base + path, data=body, headers={"X-GQE": "1"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_bulk_flag_endpoint(self):
        body = json.dumps({"filters": [{"field": "name", "op": "~", "value": "katamari"}], "flag": "favorite", "value": True}).encode()
        code, out, _ = self.post("/api/flag_bulk", body)
        self.assertEqual((code, json.loads(out)), (200, {"games": 1}))
        q = json.loads(self.post("/api/query", json.dumps({"filters": [{"field": "fav", "op": "=", "value": "yes"}]}).encode())[1])
        self.assertEqual(q["total"], 2)                                                     # both regional releases
        self.post("/api/flag_bulk", json.dumps({"filters": [], "flag": "favorite", "value": False}).encode())
        code, out, _ = self.post("/api/flag_bulk", json.dumps({"filters": [], "flag": "bogus", "value": True}).encode())
        self.assertEqual(code, 400)

    def test_download_then_restore_and_running_job_guard(self):
        code, data, hdr = self.post("/api/backup_download")
        self.assertEqual(code, 200)
        self.assertIn("attachment", hdr["Content-Disposition"])
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
        info = json.loads(urllib.request.urlopen(urllib.request.Request(self.base + "/api/db_info")).read())
        self.assertEqual(info["releases"], 8)
        # a running background job blocks restore
        self.jobs.start("slow", lambda c, p: time.sleep(0.6))
        code, out, _ = self.post("/api/restore", data)
        self.assertEqual(code, 400)
        self.assertIn("still running", json.loads(out)["error"])
        time.sleep(0.9)
        # wipe some data, then restore through the API
        c = sqlite3.connect(self.path)
        c.execute("DELETE FROM releases")
        c.commit()
        c.close()
        code, out, _ = self.post("/api/restore", data)
        self.assertEqual(code, 200, out)
        self.assertEqual(json.loads(out)["releases"], 8)
        q = json.loads(self.post("/api/query", json.dumps({"filters": [], "page_size": 5}).encode())[1])
        self.assertEqual(q["total"], 8)
        code, out, _ = self.post("/api/restore", b"nonsense")
        self.assertEqual(code, 400)


# ------------------------------------------------------------------ Wikidata
def wd_fake():
    def b(item, label, date=None, prec=None, qp=None):
        r = {"item": {"value": f"http://www.wikidata.org/entity/{item}"}, "label": {"value": label}}
        if date:
            r["date"], r["prec"] = {"value": date}, {"value": str(prec)}
        if qp:
            r["qp"] = {"value": f"http://www.wikidata.org/entity/{qp}"}
        return r

    calls = []

    def handler(method, url, body):
        q = urllib.parse.unquote_plus(url)
        calls.append(q)
        if "COUNT(" in q:
            n = 5000 if '"PlayStation 2"' in q else 3
            return {"results": {"bindings": [{"p": {"value": "http://www.wikidata.org/entity/Q10680"}, "n": {"value": str(n)}}]}}
        assert "wd:Q10680" in q
        return {"results": {"bindings": [
            b("Q1", "Katamari Damacy", "2004-03-18T00:00:00Z", 11, "Q10680"),
            b("Q1", "Katamari Damacy", "2004-09-24T00:00:00Z", 11, "Q10680"),
            b("Q1", "Katamari Damacy", "2003-01-01T00:00:00Z", 9, "Q999"),      # earlier, but another platform
            b("Q2", "Ratchet & Clank", "2002-01-01T00:00:00Z", 9),              # year-only precision
            b("Q3", "Okami"),                                                    # known game, no date
            b("Q4", "Ico", "2000-01-01T00:00:00Z", 8, "Q10680"),                # decade precision: ignored
        ]}}

    from tests.test_sources import Fake
    return Fake([("https://query.wikidata.org/sparql", handler)]), calls


class Wikidata(unittest.TestCase):
    def test_fmt_date(self):
        self.assertEqual(wikidata.fmt_date("2004-03-18T00:00:00Z", 11), "2004-03-18")
        self.assertEqual(wikidata.fmt_date("2004-03-01T00:00:00Z", 10), "2004-03")
        self.assertEqual(wikidata.fmt_date("2004-01-01T00:00:00Z", 9), "2004")
        self.assertIsNone(wikidata.fmt_date("2000-01-01T00:00:00Z", 8))

    def test_console_years_without_any_key(self):
        fake, calls = wd_fake()
        conn = db.connect(":memory:")
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        ids = [r["id"] for r in query.select(conn)]
        ctx = Context(conn, Http(transport=fake, sleep=lambda s: None), {})
        out = service.refresh_fields(conn, ctx, ["release_date", "orig_year"], ids, "wikidata")
        self.assertEqual(out["updated"], 6)                                              # 2 Katamari + Ratchet, x2 fields
        k = query.select(conn, ["name~katamari"])
        self.assertTrue(all(r["release_date"] == "2004-03-18" and r["orig_year"] == 2003 for r in k))
        rc = query.select(conn, ["name~ratchet"])[0]
        self.assertEqual((rc["release_date"], rc["year"]), ("2002", 2002))              # year-only stays year-only
        self.assertEqual(query.count(conn, ["name~okami", "year!=unknown"]), 0)         # no date -> stays blank
        self.assertEqual(query.count(conn, ["name~ico", "year!=unknown"]), 0)           # decade precision ignored
        # the headline use case, with no API key at all
        self.assertEqual(query.count(conn, ["platform=ps2", "year=2004", "size<2gb"]), 2)
        service.refresh_fields(conn, ctx, ["release_date"], ids, "wikidata")
        self.assertEqual(sum("COUNT(" in c for c in calls), 1)                          # platform id remembered

    def test_unresolvable_platform_is_skipped_not_guessed(self):
        from tests.test_sources import Fake
        fake = Fake([("https://query.wikidata.org/sparql", lambda m, u, b: {"results": {"bindings": [
            {"p": {"value": "http://www.wikidata.org/entity/Q1"}, "n": {"value": "3"}}]}})])
        conn = db.connect(":memory:")
        service.build_from_records(conn, RedumpIngestor(SAMPLE))
        ids = [r["id"] for r in query.select(conn)]
        out = service.refresh_fields(conn, Context(conn, Http(transport=fake, sleep=lambda s: None), {}),
                                     ["orig_year"], ids, "wikidata")
        self.assertEqual(out["updated"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
