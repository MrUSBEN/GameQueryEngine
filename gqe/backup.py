"""Backup / transfer: one zip = a consistent snapshot of the whole database plus a manifest.
Restore validates the file first and keeps a safety copy of what it replaces."""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from . import APP_ID, __version__, db
from .claims import now_iso


class BackupError(ValueError):
    pass


def _counts(conn) -> dict:
    return {"games": conn.execute("SELECT COUNT(*) FROM games").fetchone()[0],
            "releases": conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0],
            "claims": conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]}


def make_zip(conn: sqlite3.Connection) -> bytes:
    tmp = Path(tempfile.mkdtemp(prefix="gd-bak-"))
    try:
        snap = tmp / f"{APP_ID}.db"
        out = sqlite3.connect(str(snap))
        try:
            conn.backup(out)
        finally:
            out.close()
        manifest = {"app": APP_ID, "app_version": __version__, "created": now_iso(),
                    "schema_version": int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]),
                    **_counts(conn)}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(snap, f"{APP_ID}.db")
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
        return buf.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _extract_db(data: bytes, dest: Path) -> None:
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                name = next((n for n in z.namelist() if n.lower().endswith(".db")), None)
                if not name:
                    raise BackupError("That zip doesn't contain a database (.db) file.")
                dest.write_bytes(z.read(name))
        except zipfile.BadZipFile:
            raise BackupError("That file is not a valid zip.") from None
    elif data[:16] == b"SQLite format 3\x00":
        dest.write_bytes(data)
    else:
        raise BackupError("That doesn't look like a backup from this app (expected .zip or .db).")


def restore(db_path: Path, data: bytes) -> dict:
    """Replace the database at db_path with the backup. Returns counts of what was restored."""
    tmp = Path(tempfile.mkdtemp(prefix="gd-res-"))
    try:
        cand = tmp / "candidate.db"
        _extract_db(data, cand)
        src = sqlite3.connect(str(cand))
        try:
            try:
                if src.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise BackupError("The backup file is damaged (integrity check failed).")
                ver = src.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                counts = _counts(src)
            except sqlite3.DatabaseError as e:
                raise BackupError(f"Not a usable backup: {e}") from None
            if not ver or int(ver[0]) > db.SCHEMA_VERSION:
                raise BackupError("This backup was made by a newer version of the app. Update the app first; "
                                  "nothing was changed.")
            live = db.connect(db_path)                       # also guarantees the live db exists/is migrated
            try:
                safety = db.backup_database(live, db_path, db.SCHEMA_VERSION, keep=10) \
                    if live.execute("SELECT COUNT(*) FROM games").fetchone()[0] else None
                src.backup(live)                             # overwrite live content page by page
                live.commit()
            finally:
                live.close()
        finally:
            src.close()
        db.connect(db_path).close()                          # migrate an older backup + re-resolve
        return {**counts, "backup_version": int(ver[0]), "safety_copy": safety.name if safety else None}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def db_info(db_path: Path) -> dict:
    conn = db.connect(db_path, init=False)
    try:
        counts = _counts(conn)
    finally:
        conn.close()
    folder = db_path.parent / "backups"
    return {"path": str(db_path), "folder": str(db_path.parent), "size": db_path.stat().st_size,
            "safety_copies": sorted(p.name for p in folder.glob("*.db"))[-5:] if folder.exists() else [], **counts}
