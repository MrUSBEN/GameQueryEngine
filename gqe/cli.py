"""Optional command line. Normal use is just: double-click the launcher (= `gqe`)."""
from __future__ import annotations

import argparse

from . import db, paths, query, service
from .adapters.redump import RedumpIngestor


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gqe", description="Game Query Engine - local game research database")
    ap.add_argument("--db", help="database file (default: <project>/data/<app>.db or $<APP>_DB)")
    sub = ap.add_subparsers(dest="cmd")
    ui = sub.add_parser("ui", help="open the web UI (default)")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    b = sub.add_parser("build", help="import a Redump .dat file without the UI")
    b.add_argument("file")
    b.add_argument("--platform")
    b.add_argument("--include-variants", action="store_true")
    sub.add_parser("stats", help="show data coverage per platform")
    args = ap.parse_args(argv)

    if args.cmd in (None, "ui"):
        from .server import serve
        serve(args.db, getattr(args, "port", 8765), not getattr(args, "no_browser", False))
        return 0
    notice = paths.prepare()
    if notice:
        print(notice)
    conn = db.connect(args.db)
    if args.cmd == "build":
        r = service.build_from_records(conn, RedumpIngestor(args.file, args.platform, args.include_variants),
                                       lambda m, f=None: print(m))
        print(f"Imported {r['records']} releases from {r['source']}")
    elif args.cmd == "stats":
        for row in query.stats(conn):
            print(row)
    return 0
