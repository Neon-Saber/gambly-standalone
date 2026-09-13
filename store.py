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
import contextlib
import json
import os
import time
import uuid
from pathlib import Path

import requests

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:  # Windows - no fcntl, local-mode locking becomes a no-op there
    _HAS_FCNTL = False

_LOCAL_BASE = Path(__file__).parent
_LOCK_DIR = _LOCAL_BASE / ".locks"
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


# ---------------- cross-process locking ----------------
# THE PROBLEM: every caller does load() -> mutate the dict in Python -> save().
# That round trip is NOT atomic. If two of those overlap - the bot handling a
# !give at the same moment the dashboard saves a settings change, or the
# hourly background loop touching config.json while someone runs !testmode -
# whichever one calls save() SECOND wins and silently throws away whatever the
# first one wrote, because it started from a copy of the data that was already
# stale by the time it saves. That's the "I did the thing but it didn't stick"
# bug class (testmode not turning off, !give not actually moving chips).
#
# THE FIX: wrap the whole load-mutate-save span for a given key in a lock, so
# a second load() for that same key has to wait until the first transaction's
# save() has finished:
#
#   with store.locked(econ_file):
#       econ = store.load(econ_file)
#       ...mutate...
#       store.save(econ_file, econ)
#
# Local-file mode uses a real OS-level lock (fcntl.flock on a .lock file) -
# this works across the bot process and every dashboard worker as long as
# they're on the same machine/container. Upstash mode uses a short-lived
# SET-if-not-exists token as a distributed lock, so it also works across
# separate machines (bot on a VM, dashboard on Render, etc).
#
# Both are BEST-EFFORT: if a lock can't be acquired within a few seconds
# (crashed process left a lock file behind, network hiccup with Upstash),
# this proceeds anyway rather than hanging the bot/dashboard forever. That's
# a deliberate trade-off - a rare missed lock beats an actual outage.
_UPSTASH_LOCK_TTL_MS = 8000
_LOCK_WAIT_TIMEOUT_S = 6
_LOCK_POLL_S = 0.05


def _local_lock_path(key):
    _LOCK_DIR.mkdir(exist_ok=True)
    return _LOCK_DIR / f"{key}.lock"


@contextlib.contextmanager
def _local_file_lock(key):
    if not _HAS_FCNTL:
        yield
        return
    fh = open(_local_lock_path(key), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _upstash_acquire_lock(key):
    lock_key = f"lock:{key}"
    token = uuid.uuid4().hex
    deadline = time.time() + _LOCK_WAIT_TIMEOUT_S
    while time.time() < deadline:
        try:
            r = requests.post(_upstash_url(), headers=_headers(),
                               json=["SET", lock_key, token, "NX", "PX", _UPSTASH_LOCK_TTL_MS],
                               timeout=10)
            r.raise_for_status()
            if r.json().get("result") == "OK":
                return token
        except Exception:
            return None  # can't reach Upstash to lock at all - proceed unlocked rather than hang
        time.sleep(_LOCK_POLL_S)
    return None  # someone else held it the whole time we waited - proceed anyway, best-effort


def _upstash_release_lock(key, token):
    if not token:
        return
    lock_key = f"lock:{key}"
    try:
        # only release if we still hold it - if our TTL already expired and
        # someone else grabbed the lock, don't delete THEIRS out from under them
        r = requests.post(_upstash_url(), headers=_headers(), json=["GET", lock_key], timeout=10)
        if r.ok and r.json().get("result") == token:
            requests.post(_upstash_url(), headers=_headers(), json=["DEL", lock_key], timeout=10)
    except Exception:
        pass  # TTL will clear it out on its own shortly either way


@contextlib.contextmanager
def locked(name_or_path):
    """Advisory lock around a load-mutate-save transaction for one stored
    key. See the module comment above for why this exists and how to use it."""
    key = _key_for(name_or_path)
    if _enabled():
        token = _upstash_acquire_lock(key)
        try:
            yield
        finally:
            _upstash_release_lock(key, token)
    else:
        with _local_file_lock(key):
            yield
