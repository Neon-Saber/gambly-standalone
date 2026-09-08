"""
Single source of truth for what a per-guild config entry in config.json
looks like, and its defaults. Both bot.py (the Discord bot) and
dashboard/data.py (the Flask dashboard) import THIS module instead of each
keeping their own copy of the schema - before this existed the two had
quietly drifted (the bot's version stamped the live guild name on every
call, the dashboard's didn't; new fields would only need to be added in
one place now instead of two).

No discord.py import here on purpose - the dashboard process shouldn't
need the Discord library just to read/write JSON, and this module needs to
work identically for both processes.
"""
import json
import os
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
CFG_FILE = BASE_DIR / "config.json"

# one (enabled-flag key, channel-id key, .env fallback var) tuple per log
# type. the .env var is only ever consulted the FIRST time a guild's config
# is created (see ensure_guild below) - after that, the dashboard is the
# source of truth and .env is ignored for that guild. a log type starts
# enabled if (and only if) its .env var was actually set to something, so a
# fresh install with no channel IDs configured doesn't try to log into
# channel 0/None and silently fail.
LOG_TYPES = {
    "message": "MESSAGE_LOG_CHANNEL_ID",
    "mod": "MOD_LOG_CHANNEL_ID",
    "report": "REPORT_LOG_CHANNEL_ID",
    "withdraw": "WITHDRAW_LOG_CHANNEL_ID",
    "deposit": "DEPOSIT_LOG_CHANNEL_ID",
    "ticket": "TICKET_LOG_CHANNEL_ID",
}


def _env_int(name):
    """Read an integer ID out of the environment, tolerating blank/missing/
    garbage values instead of throwing - every ID in .env is optional."""
    raw = (os.getenv(name) or "").strip()
    if not raw.isdigit():
        return None
    return int(raw)


def load_cfg():
    if not CFG_FILE.exists():
        return {}
    with open(CFG_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_cfg(cfg):
    tmp = CFG_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CFG_FILE)


def ensure_guild(all_cfg, gid, name=None):
    """Get (creating with defaults if needed) the config entry for guild
    `gid` inside the `all_cfg` dict, mutating `all_cfg` in place and
    returning the guild's entry. Does NOT save to disk - callers that
    actually change something are responsible for calling save_cfg()
    afterwards, same as the rest of this project's config handling.

    `gid` can be an int or str id. `name` is optional - pass it when you
    have a live discord.Guild handy (the bot always does; the dashboard
    only sometimes does) to keep the stored name fresh.
    """
    gid = str(gid)
    if gid not in all_cfg:
        all_cfg[gid] = {}
    g = all_cfg[gid]

    if name is not None:
        g["name"] = name
    g.setdefault("name", None)

    # ---- casino / economy (unchanged from the original project) ----
    g.setdefault("prefix", "!")
    g.setdefault("manager_role", None)
    g.setdefault("managers", [])
    g.setdefault("banned", [])
    g.setdefault("jackpot", 500)
    g.setdefault("tax_pct", 0)
    g.setdefault("server_pot", 0)
    g.setdefault("event", None)
    g.setdefault("announce_channel", None)
    g.setdefault("aliases", {})
    g.setdefault("week", {"period_start": time.time(), "biggest_win": None, "activity": {}})
    g.setdefault("bounties", {})
    g.setdefault("lottery", {"pot": 0, "tickets": {}, "period_start": time.time()})
    g.setdefault("testmode", False)

    # ---- moderation / staff ----
    # anyone with this role can use mod + ticket-staff commands, in addition
    # to anyone with the native Discord permission for a given command (and
    # server Administrators, always). separate from "manager_role" above,
    # which only governs the casino/economy commands.
    g.setdefault("staff_role_id", _env_int("STAFF_ROLE_ID"))

    # ---- tickets ----
    g.setdefault("ticket_category_id", _env_int("TICKET_CATEGORY_ID"))
    g.setdefault("ticket_ping_role_id", _env_int("TICKET_PING_ROLE_ID"))

    # ---- logging (message / mod / report / withdraw / deposit / ticket) ----
    for log_type, env_var in LOG_TYPES.items():
        env_channel = _env_int(env_var)
        g.setdefault(f"log_{log_type}_channel", env_channel)
        g.setdefault(f"log_{log_type}_enabled", env_channel is not None)

    return g


def known_guild_ids(cfg=None):
    return set((cfg if cfg is not None else load_cfg()).keys())
