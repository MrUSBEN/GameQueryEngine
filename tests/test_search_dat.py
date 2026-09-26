import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import claims, db, export, query, service
from gqe.adapters.redump import RedumpIngestor, parse_any_date
from gqe.ingest import search_norm, upsert_release


class TitleSearch(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        for name in ("ARK: Survival Evolved", "Half-Life 2", "Pokémon Stadium", "Ratchet & Clank", "Park Survival Island"):
            upsert_release(self.conn, source="test", source_key=name, name=name, platform="pc")

    def names(self, *f):
        return sorted(r["name"] for r in query.select(self.conn, list(f)))

    def test_punctuation_accents_and_ampersands_do_not_break_search(self):
        self.assertEqual(search_norm("ARK: Survival Evolved"), "ark survival evolved")
        self.assertIn("ARK: Survival Evolved", self.names('name~"ark survival"'))               # the reported case
        self.assertEqual(self.names('name~"half life 2"'), ["Half-Life 2"])
        self.assertEqual(self.names("name~pokemon"), ["Pokémon Stadium"])
        self.assertEqual(self.names('name~"ratchet and clank"'), ["Ratchet & Clank"])
        self.assertEqual(self.names('name~"ratchet & clank"'), ["Ratchet & Clank"])            # typed the original way still works
        self.assertEqual(self.names("name~ARK:"), ["ARK: Survival Evolved"])

    def test_exclusion_uses_the_same_matching(self):
        rest = self.names('name!~"ark survival"')
        self.assertNotIn("ARK: Survival Evolved", rest)
        self.assertIn("Half-Life 2", rest)

    def test_plain_substring_search_is_unchanged(self):
        self.assertEqual(self.names("name~ratchet"), ["Ratchet & Clank"])
        self.assertEqual(len(self.names("name~zzzz")), 0)


class Migration3(unittest.TestCase):
    def test_v2_database_is_upgraded_and_backfilled_with_a_backup(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            path = tmp / "v2.db"
            c = sqlite3.connect(path)
            c.executescript(db.TABLES)
            c.execute("CREATE TABLE source_state(source TEXT NOT NULL, key TEXT NOT NULL, value TEXT, updated_at TEXT, PRIMARY KEY(source, key))")
            c.execute("INSERT INTO meta VALUES('schema_version','2')")
            c.execute("INSERT INTO games(id,name,name_norm) VALUES(1,'ARK: Survival Evolved','arksurvivalevolved')")
            c.execute("INSERT INTO releases(id,game_id,platform) VALUES(1,1,'pc')")
            c.commit()
            c.close()
            conn = db.connect(path)
            self.assertEqual(conn.execute("SELECT search_name FROM games").fetchone()[0], "ark survival evolved")
            conn.execute("SELECT * FROM steam_checked")                                       # new table exists
            self.assertEqual(query.count(conn, ['name~"ark survival"']), 1)
            self.assertEqual(len(list((tmp / "backups").glob("v2-v2-*.db"))), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DatDatesAndGenres(unittest.TestCase):
    DAT = """<?xml version="1.0"?>
<datafile><header><name>Sony - PlayStation 2</name></header>
 <game name="With Release Tag (USA)"><release name="With Release Tag (USA)" region="USA" date="2004-03-18"/><genre>Puzzle</genre>
   <rom name="a.iso" size="1000000000" serial="SLUS-1"/></game>
 <game name="With Year Element (Europe)"><year>2005</year><rom name="b.iso" size="2000000000"/></game>
 <game name="With Attribute (Japan)" releaseyear="2003/07/09" genre="Action / Adventure"><rom name="c.iso" size="3000000000"/></game>
 <game name="Two Dates (USA)"><release date="2006-05-01"/><release date="2004-12-25"/><rom name="d.iso" size="500000000"/></game>
 <game name="Bad Date (USA)"><releasedate>sometime</releasedate><rom name="e.iso" size="400000000"/></game>
 <game name="Plain (USA)"><rom name="f.iso" size="300000000"/></game>
 <game name="Disc Game (USA) (Disc 1)"><year>2001</year><rom name="g1.iso" size="100"/></game>
 <game name="Disc Game (USA) (Disc 2)"><rom name="g2.iso" size="100"/></game>
</datafile>"""

    def test_parse_any_date(self):
        for text, want in (("2004-03-18", "2004-03-18"), ("2004/3/18", "2004-03-18"), ("20040318", "2004-03-18"),
                           ("2004-03", "2004-03"), ("2004", "2004"), ("  2004 ", "2004"), ("sometime", None),
                           ("1900", None), ("2004-13-01", None), ("", None), (None, None)):
            self.assertEqual(parse_any_date(text), want, text)

    def test_dates_and_genres_from_the_dat_reach_the_database(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "x.dat"
            f.write_text(self.DAT)
            conn = db.connect(":memory:")
            service.build_from_records(conn, RedumpIngestor(f))
            rows = {r["name"]: r for r in query.select(conn)}
            self.assertEqual(rows["With Release Tag"]["release_date"], "2004-03-18")
            self.assertEqual(rows["With Release Tag"]["genres"], "|puzzle|")
            self.assertEqual(rows["With Year Element"]["year"], 2005)
            self.assertEqual(rows["With Attribute"]["release_date"], "2003-07-09")
            self.assertEqual(rows["With Attribute"]["genres"], "|action|adventure|")
            self.assertEqual(rows["Two Dates"]["release_date"], "2004-12-25")                  # earliest wins
            self.assertIsNone(rows["Bad Date"]["release_date"])                                  # junk is ignored, not guessed
            self.assertIsNone(rows["Plain"]["year"])
            self.assertEqual(rows["Disc Game"]["year"], 2001)                                    # first disc's date used for the game
            self.assertEqual(rows["Disc Game"]["discs"], 2)
            self.assertEqual(query.count(conn, ["platform=ps2", "year=2004", "size<2gb"]), 2)
            service.build_from_records(conn, RedumpIngestor(f))                                  # re-import: idempotent
            self.assertEqual(query.count(conn), 7)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reimporting_a_dat_adds_dates_to_games_imported_without_them(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            conn = db.connect(":memory:")
            plain = tmp / "plain.dat"
            plain.write_text(self.DAT.replace("<year>2005</year>", "").replace("<year>2001</year>", ""))
            service.build_from_records(conn, RedumpIngestor(plain))
            self.assertIsNone(query.select(conn, ["name~'With Year Element'"])[0]["year"])
            full = tmp / "full.dat"
            full.write_text(self.DAT)
            service.build_from_records(conn, RedumpIngestor(full))
            self.assertEqual(query.select(conn, ["name~'With Year Element'"])[0]["year"], 2005)
            self.assertEqual(query.count(conn), 7)                                               # no duplicates
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ScoreSource(unittest.TestCase):
    def test_source_is_visible_filterable_and_exportable(self):
        conn = db.connect(":memory:")
        a = upsert_release(conn, source="t", source_key="a", name="Alpha", platform="pc")
        b = upsert_release(conn, source="t", source_key="b", name="Beta", platform="pc")
        claims.record_claim(conn, a, "score", "igdb", 80)
        claims.record_claim(conn, b, "score", "steamspy", 90)
        self.assertEqual([r["name"] for r in query.select(conn, ["score_source=steamspy"])], ["Beta"])
        rows = query.select(conn, [], "name")
        self.assertEqual([r["score_source"] for r in rows], ["igdb", "steamspy"])
        full = json.loads(export.render(rows, "json", "full"))
        self.assertEqual([r["score_source"] for r in full], ["igdb", "steamspy"])


if __name__ == "__main__":
    unittest.main()
