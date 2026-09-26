import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gqe import adapters, claims, db, filters, query, service
from gqe.adapters import Context, igdb
from gqe.adapters.redump import RedumpIngestor
from gqe.http import Http
from gqe.ingest import upsert_release
from tests.test_sources import CFG, Fake, ctx_for, igdb_fake, ts

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_ps2.dat"


def mixed_db():
    conn = db.connect(":memory:")
    service.build_from_records(conn, RedumpIngestor(SAMPLE))
    pc = upsert_release(conn, source="gog", source_key="1", name="Old PC Game", platform="pc")
    claims.record_claim(conn, pc, "genres", "gog", "indie, puzzle")
    claims.record_claim(conn, pc, "drm", "gog", "drm-free")
    pc2 = upsert_release(conn, source="gog", source_key="2", name="Plain PC Game", platform="pc")   # no genres, no region
    return conn, pc, pc2


class NegativeFilters(unittest.TestCase):
    def setUp(self):
        self.conn, self.pc, self.pc2 = mixed_db()

    def names(self, *f):
        return sorted({r["name"] for r in query.select(self.conn, list(f))})

    def test_exclude_platform_gives_console_only(self):
        rows = query.select(self.conn, ["platform!=pc"])
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(r["platform"] == "ps2" for r in rows))
        self.assertEqual(query.count(self.conn, ["platform!=pc,ps2"]), 0)

    def test_negatives_keep_rows_where_the_field_is_empty(self):
        # region is empty for PC rows: "not USA" must still show them
        self.assertIn("Old PC Game", self.names("region!~USA"))
        self.assertNotIn("Okami", self.names("region!~USA"))
        self.assertIn("Katamari Damacy", self.names("region!~USA"))                      # has a Europe release
        # genre is empty for Plain PC Game: "not indie" keeps it, drops the indie one
        g = self.names("genre!=indie")
        self.assertIn("Plain PC Game", g)
        self.assertNotIn("Old PC Game", g)
        self.assertIn("Old PC Game", self.names("genre:indie"))
        self.assertIn("Plain PC Game", self.names("drm!=drm-free"))                      # unknown DRM is 'not drm-free'
        self.assertNotIn("Old PC Game", self.names("drm!=drm-free"))

    def test_title_does_not_contain_any_of(self):
        n = self.names("name!~katamari,okami")
        self.assertNotIn("Okami", n)
        self.assertNotIn("Katamari Damacy", n)
        self.assertIn("Bully", n)
        self.assertEqual(filters.parse_filters(["name nhas ico"])[0].op, "!~")
        self.assertEqual(filters.parse_filters(["name!~ico"])[0].op, "!~")

    def test_combined_include_and_exclude_and_numeric_unchanged(self):
        self.assertEqual(self.names("platform=ps2", "region!~Europe", "size<2gb"), ["Ico", "Katamari Damacy"])
        self.assertEqual(query.count(self.conn, ["size!=unknown", "platform=pc"]), 0)     # numeric NULL rule unchanged


# ------------------------------------------------------------------ IGDB catalog import
def igdb_catalog(games):
    base = igdb_fake()
    seen = []

    def games_handler(method, url, body):
        text = (body or b"").decode()
        seen.append(text)
        if "game_type = 0; limit 1" in text:
            return [{"id": 1}] if not getattr(games_handler, "broken", False) else []
        return games if "platforms = (6)" in text else []
    routes = [r for r in base.routes if r[0] not in ("https://api.igdb.com/v4/games", "https://api.igdb.com/v4/platforms")]
    routes.append(("https://api.igdb.com/v4/platforms", [{"id": 6, "name": "PC (Microsoft Windows)"}]))
    routes.insert(0, ("https://api.igdb.com/v4/games", games_handler))
    return Fake(routes), seen, games_handler


class IgdbImport(unittest.TestCase):
    def games(self):
        return [
            {"id": 100, "name": "Half-Life", "first_release_date": ts(1998, 11, 19), "total_rating": 91.2,
             "genres": [{"name": "Shooter"}], "release_dates": [{"platform": 6, "date": ts(1998, 11, 19)}]},
            {"id": 101, "name": "Some GOG Game: Enhanced Edition", "first_release_date": ts(2001, 5, 1),
             "alternative_names": [{"name": "Some GOG Game"}], "genres": [{"name": "Role-playing (RPG)"}],
             "total_rating": 70.0, "release_dates": [{"platform": 6, "date": ts(2001, 5, 1)}]},
            {"id": 102, "name": "Brand New Thing", "first_release_date": ts(2015, 3, 3), "total_rating": 55.5,
             "genres": [], "release_dates": []},
        ]

    def setUp(self):
        self.conn = db.connect(":memory:")
        self.existing = upsert_release(self.conn, source="gog", source_key="9", name="Some GOG Game", platform="pc",
                                       size_bytes=1_000_000_000, size_conf="exact")

    def run_import(self, fake=None, **kw):
        fake = fake or igdb_catalog(self.games())[0]
        ctx = ctx_for(self.conn, fake, CFG)
        return service.build_from_records(self.conn, igdb.IgdbIngestor(ctx, **kw)), fake

    def test_adds_new_games_skips_existing_and_keeps_your_data(self):
        out, fake = self.run_import()
        self.assertEqual((out["records"], out["added"], out["matched"]), (3, 2, 1))
        self.assertEqual(query.count(self.conn, ["platform=pc"]), 3)                     # 1 existing + 2 new, no duplicate
        hl = query.select(self.conn, ["name=Half-Life"])[0]
        self.assertEqual((hl["orig_year"], hl["release_date"], hl["score"], hl["genres"]), (1998, "1998-11-19", 91.2, "|shooter|"))
        self.assertIsNone(hl["size_bytes"])                                              # IGDB has no sizes
        old = query.select(self.conn, [f"id={self.existing}"])[0]                        # matched through its alias
        self.assertEqual((old["size_bytes"], old["size_source"], old["genres"], old["orig_year"]),
                         (1_000_000_000, "gog", "|rpg|", 2001))
        # the whole point: a real filter over the imported library
        self.assertEqual(query.count(self.conn, ["platform=pc", "year=1990..1999", "score>=60", "genre:shooter"]), 1)

    def test_rerun_is_idempotent(self):
        self.run_import()
        out, _ = self.run_import()
        self.assertEqual(query.count(self.conn, ["platform=pc"]), 3)
        self.assertEqual(out["added"], 0)

    def test_only_real_released_games_are_requested(self):
        _, fake = self.run_import()[0], None
        fake = igdb_catalog(self.games())
        self.run_import(fake[0])
        body = next(b for b in fake[1] if "sort id asc" in b)
        self.assertIn("game_type = (0,4,8,9,10,11)", body)                               # no DLC / mods / bundles
        self.assertIn("first_release_date != null", body)                                # released only
        fake2 = igdb_catalog(self.games())
        self.run_import(fake2[0], include_unreleased=True)
        self.assertNotIn("first_release_date != null", next(b for b in fake2[1] if "sort id asc" in b))

    def test_refuses_to_import_if_type_ids_stop_working(self):
        fake, seen, handler = igdb_catalog(self.games())
        handler.broken = True
        with self.assertRaises(LookupError) as cm:
            self.run_import(fake)
        self.assertIn("type ids may have changed", str(cm.exception))
        self.assertEqual(query.count(self.conn, ["platform=pc"]), 1)                     # nothing added

    def test_update_source_needs_keys_and_uses_platform_choice(self):
        fake = igdb_catalog(self.games())[0]
        with self.assertRaises(LookupError):
            service.update_source(self.conn, ctx_for(self.conn, fake, {}), "igdb_import", {})
        out = service.update_source(self.conn, ctx_for(self.conn, fake, CFG), "igdb_import", {"platforms": ["pc"]})
        self.assertEqual(out["added"], 2)


# ------------------------------------------------------------------ refresh all / only fill empty
class OnlyMissing(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        service.build_from_records(self.conn, RedumpIngestor(SAMPLE))
        self.seen_rows, outer = [], self

        class Fake_(adapters.Refresher):
            name, fields = "fake", frozenset({"score", "price"})

            def fetch(self, rows, fields, links):
                outer.seen_rows.append((sorted(r["name"] for r in rows), list(fields)))
                for r in rows:
                    for f in fields:
                        yield adapters.Update(r["id"], f, 50.0 if f == "score" else 3.0)
        adapters.REFRESHERS["fake"] = Fake_

    def tearDown(self):
        adapters.REFRESHERS.pop("fake", None)

    def ids(self, *f):
        return [r["id"] for r in query.select(self.conn, list(f))]

    def test_existing_values_are_kept_and_complete_games_are_not_looked_up(self):
        okami = self.ids("name=Okami")[0]
        bully = self.ids("name=Bully")[0]
        claims.record_claim(self.conn, okami, "score", "manual", 99)                     # you typed a score
        claims.record_claim(self.conn, bully, "score", "igdb", 80)
        claims.record_claim(self.conn, bully, "price", "steam", 9.5)                     # Bully is complete
        ctx = ctx_for(self.conn, Fake([]), {})
        out = service.refresh_fields(self.conn, ctx, ["score", "price"], self.ids(), "fake", only_missing=True)
        looked_up = self.seen_rows[0][0]
        self.assertNotIn("Bully", looked_up)                                             # nothing missing -> not asked
        self.assertIn("Okami", looked_up)
        r = query.select(self.conn, [f"id={okami}"])[0]
        self.assertEqual((r["score"], r["price"]), (99.0, 3.0))                          # kept my 99, filled empty price
        b = query.select(self.conn, [f"id={bully}"])[0]
        self.assertEqual((b["score"], b["price"], b["score_source"] if "score_source" in b else "igdb"), (80.0, 9.5, "igdb"))
        self.assertFalse(self.conn.execute("SELECT 1 FROM claims WHERE field='score' AND source='fake' AND row_id=(SELECT game_id FROM releases WHERE id=?)", (okami,)).fetchone())
        self.assertGreater(out["updated"], 0)

    def test_without_the_flag_values_are_overwritten_by_precedence(self):
        rid = self.ids("name=Okami")[0]
        claims.record_claim(self.conn, rid, "price", "steam", 9.5)
        service.refresh_fields(self.conn, ctx_for(self.conn, Fake([]), {}), ["price"], [rid], "fake")
        self.assertEqual(query.select(self.conn, [f"id={rid}"])[0]["price"], 3.0)        # newest price wins as before

    def test_all_columns_means_only_empty_cells_and_estimate_agrees(self):
        ids = self.ids()
        est = service.estimate_refresh(self.conn, ctx_for(self.conn, Fake([]), {}), ["all"], ids, "fake")
        self.assertEqual(est["to_look_up"], len(ids))
        for rid in ids:                                                                   # fill everything the fake can
            claims.record_claim(self.conn, rid, "price", "steam", 1)
            claims.record_claim(self.conn, rid, "score", "igdb", 1)
        est = service.estimate_refresh(self.conn, ctx_for(self.conn, Fake([]), {}), ["all"], ids, "fake")
        self.assertEqual(est["to_look_up"], 0)                                           # nothing empty any more
        self.assertIn("score", service.all_fields())
        out = service.refresh_fields(self.conn, ctx_for(self.conn, Fake([]), {}), ["all"], ids[:1], "fake")
        self.assertEqual(out["fields"], service.all_fields())


class Flags(unittest.TestCase):
    def setUp(self):
        self.conn, self.pc, self.pc2 = mixed_db()

    def test_bulk_mark_by_filter_is_game_level(self):
        out = service.set_user_flags(self.conn, filters.parse_filters(["name~katamari", "region=USA"]), "played", True)
        self.assertEqual(out["games"], 1)
        rows = query.select(self.conn, ["played=yes"])
        self.assertEqual(sorted(r["region"] for r in rows), ["Europe", "USA"])            # the Europe release is the same game
        self.assertEqual(query.count(self.conn, ["played=no"]), 8)                         # 10 releases, 2 of them Katamari
        service.set_user_flags(self.conn, filters.parse_filters(["name~katamari"]), "played", False)
        self.assertEqual(query.count(self.conn, ["played=yes"]), 0)

    def test_marking_some_games_never_touches_games_that_already_have_marks(self):
        service.set_user_flags(self.conn, filters.parse_filters(["name=Okami"]), "favorite", True)        # Okami is a favorite
        service.set_user_flags(self.conn, filters.parse_filters(["name=Bully"]), "played", True)
        service.set_user_flags(self.conn, filters.parse_filters(["name~katamari"]), "played", True)
        self.assertEqual(query.count(self.conn, ["name=Okami", "fav=yes"]), 1)                            # still a favorite
        self.assertEqual(query.count(self.conn, ["name=Okami", "played=yes"]), 0)                         # not swept up
        service.set_user_flags(self.conn, filters.parse_filters(["name~katamari"]), "played", False)      # un-mark a subset
        self.assertEqual(query.count(self.conn, ["name=Bully", "played=yes"]), 1)                         # Bully's mark survives
        service.set_user_flags(self.conn, filters.parse_filters(["name=Bully"]), "favorite", False)
        self.assertEqual(query.count(self.conn, ["name=Okami", "fav=yes"]), 1)                            # Okami's survives too

    def test_bulk_mark_selection_and_independent_flags(self):
        ids = query.ids(self.conn, ["platform=pc"])
        self.assertEqual(len(ids), 2)
        out = service.set_user_flags(self.conn, [filters.Clause("id", "=", ",".join(map(str, ids)))], "favorite", True)
        self.assertEqual(out["games"], 2)
        self.assertEqual(query.count(self.conn, ["fav=yes", "platform=pc"]), 2)
        self.assertEqual(query.count(self.conn, ["played=yes"]), 0)                        # flags don't affect each other
        self.assertEqual(query.count(self.conn, ["fav=no", "platform=ps2"]), 8)
        with self.assertRaises(ValueError):
            service.set_user_flags(self.conn, [], "owned", True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
