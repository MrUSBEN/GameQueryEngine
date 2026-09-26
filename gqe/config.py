"""User settings and API keys (config.json in the user data folder, owner-only permissions)."""
from __future__ import annotations

import json
import os

from . import paths

SECRETS = {("igdb", "client_secret"), ("steam", "api_key"), ("rawg", "api_key")}


def load() -> dict:
    p = paths.config_path()
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save(cfg: dict) -> None:
    p = paths.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2), "utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def get(cfg: dict, dotted: str, default: str = "") -> str:
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur if cur is not None else default


def update(partial: dict) -> dict:
    """Merge one level deep. None = leave unchanged; "" = clear."""
    cfg = load()
    for section, values in partial.items():
        if not isinstance(values, dict):
            continue
        cur = cfg.setdefault(section, {})
        for k, v in values.items():
            if v is None:
                continue
            if v == "":
                cur.pop(k, None)
            else:
                cur[k] = v.strip() if isinstance(v, str) else v
    save(cfg)
    return cfg


def public_view(cfg: dict) -> dict:
    """What the browser is allowed to see: never the secret itself."""
    return {"igdb": {"client_id": get(cfg, "igdb.client_id"),
                     "client_secret_set": bool(get(cfg, "igdb.client_secret"))},
            "steam": {"api_key_set": bool(get(cfg, "steam.api_key"))},
            "rawg": {"api_key_set": bool(get(cfg, "rawg.api_key"))}}
