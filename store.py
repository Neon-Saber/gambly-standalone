"""
Shared storage for every JSON file this project persists (config.json,
economy.json, personal_economy.json, warnings.json, activity.json,
settings.json, levels.json).

THE PROBLEM THIS SOLVES: the bot and the dashboard are two different
processes that can run on two different machines (bot on your PC/VM,
dashboard on Render). A plain local JSON file on disk is only ever
visible to whatever's running on THAT machine - so if each process just
opens "config.json" next to itself, they're silently writing to two
completely different files that happen to share a name, and neither
process ever sees the other one's changes. That's what "dashboard save
doesn't do anything" actually was.

THE FIX: when UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are
set in .env, every load/save in this project goes through Upstash's
free Redis-over-HTTPS instead of the local disk - one JSON blob per
file, stored under a key matching the filename (e.g. "config",
"economy"). It's plain HTTPS with no persistent connection needed, so
it works identically from your PC, a VM, or Render - anywhere with
outbound internet. Both processes just need the SAME two env vars
pointed at the SAME database and they're reading/writing the same data,
live, no matter where either of them runs.

Setup (free, a couple minutes):
  1. https://console.upstash.com -> sign up -> Create Database (Redis,
     any region close to you, free tier is plenty for this).
  2. On the database's page, copy the "REST API" section's
     UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN.
  3. Put both in .env on EVERY machine that runs this project (your PC,
     a VM later, and Render's Environment tab for the dashboard) - same
     two values, everywhere.

If those two env vars aren't set, this module transparently falls back
to local JSON files exactly like this project always worked before -
nothing breaks for local-only testing, you just won't get cross-machine
sync until Upstash is configured.
"""
import json
import os
from pathlib import Path

import requests

_LOCAL_BASE = Path(__file__).parent
_WARNED = set()  # only print the "falling back to local file" warning once per key, not on every call
_backend_logged = False  # only print which backend is active once, on first real use


def _upstash_url():
    # read fresh every time instead of once at import - if this were computed
    # at module load, it would depend on whatever's already in os.environ at
    # the moment `import store` runs, which can be BEFORE load_dotenv() has
    # populated it from a .env file depending on import order elsewhere (this
    # bit the project once already - see bot.py's import order comment).
    return (os.getenv("UPSTASH_REDIS_REST_URL") or "").rstrip("/")


def _upstash_token():
    return os.getenv("UPSTASH_REDIS_REST_TOKEN") or ""


def _enabled():
    return bool(_upstash_url() and _upstash_token())


def _log_backend_once():
    global _backend_logged
    if _backend_logged:
        return
    _backend_logged = True
    if _enabled():
        print(f"[store] shared storage: Upstash ({_upstash_url()})", flush=True)
    else:
        print("[store] shared storage: LOCAL FILES ONLY - set UPSTASH_REDIS_REST_URL/TOKEN "
              "to sync across machines", flush=True)


def _headers():
    return {"Authorization": f"Bearer {_upstash_token()}"}


def _key_for(name_or_path):
    """Accepts either a short key ('config') or a Path/filename
    ('config.json', or a full Path to one) - always returns just the
    stem, so existing callers that pass a Path don't need to change."""
    return Path(str(name_or_path)).stem


def _local_path(key):
    return _LOCAL_BASE / f"{key}.json"


def _warn_once(key, action, err):
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(f"[store] couldn't reach Upstash to {action} '{key}' ({err}) - "
          f"falling back to the local file this time. If this keeps happening, "
          f"double check UPSTASH_REDIS_REST_URL/TOKEN in .env.")


def load(name_or_path, default=None):
    """Returns the stored dict for this key, or `default` (a fresh {} if
    not given) if nothing's stored yet anywhere."""
    if default is None:
        default = {}
    _log_backend_once()
    key = _key_for(name_or_path)

    if _enabled():
        try:
            r = requests.post(_upstash_url(), headers=_headers(), json=["GET", key], timeout=10)
            r.raise_for_status()
            raw = r.json().get("result")
            if raw is None:
                return default
            return json.loads(raw)
        except Exception as e:
            _warn_once(key, "read", e)
            # fall through to the local file as a safety net so a network
            # blip doesn't take down whatever feature triggered this read

    path = _local_path(key)
    if not path.exists():
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default


def save(name_or_path, data):
    _log_backend_once()
    key = _key_for(name_or_path)

    if _enabled():
        try:
            r = requests.post(_upstash_url(), headers=_headers(), json=["SET", key, json.dumps(data)], timeout=10)
            r.raise_for_status()
            return
        except Exception as e:
            _warn_once(key, "write", e)
            # fall through and also write locally so the data isn't lost -
            # it just won't be visible to the other machine until Upstash
            # is reachable again

    path = _local_path(key)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)
