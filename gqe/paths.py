"""Where user data lives.

Default: a `data/` folder INSIDE the project folder, next to the code (easy to find, copy,
back up). It is git-ignored, so `git pull` never touches it. Only if the app is pip-installed
(no project folder) does it fall back to a per-user folder ~/.<APP_ID>/.

    override the whole folder   <APP_ID>_HOME
    override just the database  <APP_ID>_DB
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from . import APP_ID

LEGACY_IDS: tuple[str, ...] = ("gamedex",)   # previous APP_IDs (their database file / folder / env vars are adopted)


def project_root() -> Path | None:
    root = Path(__file__).resolve().parent.parent
    return None if {"site-packages", "dist-packages"} & set(root.parts) else root


def _env(suffix: str) -> str | None:
    """<APP_ID>_HOME / _DB, falling back to the same variable under an older app id."""
    for i in (APP_ID, *LEGACY_IDS):
        v = os.environ.get(f"{i.upper()}_{suffix}")
        if v:
            return v
    return None


def app_dir() -> Path:
    env = _env("HOME")
    if env:
        return Path(env)
    root = project_root()
    return root / "data" if root else Path.home() / f".{APP_ID}"


def db_path() -> Path:
    env = _env("DB")
    return Path(env) if env else app_dir() / f"{APP_ID}.db"


def config_path() -> Path:
    return app_dir() / "config.json"


def cache_dir() -> Path:
    return app_dir() / "cache"


def _copy_db(src: Path, dst: Path) -> None:
    a, b = sqlite3.connect(str(src)), sqlite3.connect(str(dst))
    try:
        a.backup(b)          # consistent copy even if the source is in WAL mode
    finally:
        a.close()
        b.close()


def prepare() -> str | None:
    """Create the data folder; on first run adopt data from an earlier location.
    Copies (never moves) so the old location stays as a fallback. Returns a notice or None."""
    if _env("DB"):
        return None
    d = app_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "README.txt").write_text(
        "This folder holds YOUR data: the database, API keys (config.json), cache and backups.\n"
        "It is ignored by git, so updating the app never touches it. To move to another computer,\n"
        "use Data > Backup & transfer in the app, or copy this whole folder.\n", "utf-8") \
        if not (d / "README.txt").exists() else None
    target = d / f"{APP_ID}.db"
    for old in LEGACY_IDS:                                   # after an app rename: move db + its WAL files
        if (d / f"{old}.db").exists() and not target.exists():
            for suffix in ("", "-wal", "-shm"):
                src = d / f"{old}.db{suffix}"
                if src.exists():
                    src.rename(d / f"{APP_ID}.db{suffix}")
    if target.exists():
        return None
    for home in [Path.home() / f".{i}" for i in (APP_ID, *LEGACY_IDS)]:
        for stem in (APP_ID, *LEGACY_IDS):
            src = home / f"{stem}.db"
            if src.exists() and home.resolve() != d.resolve():
                _copy_db(src, target)
                if (home / "config.json").exists() and not (d / "config.json").exists():
                    shutil.copy2(home / "config.json", d / "config.json")
                return (f"Copied your existing data from {home} to {d}. "
                        f"The old copy was left untouched; delete it when you're happy.")
    return None
