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
import os
import time
from pathlib import Path

import store

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


def _env_id(name):
    """Read a Discord snowflake ID out of the environment as a STRING,
    tolerating blank/missing/garbage values instead of throwing - every ID
    in .env is optional.

    Deliberately a string, not an int: this value ends up round-tripping
    through the dashboard's JSON API to JavaScript at some point, and JS
    numbers are IEEE754 doubles that lose precision above 2^53 - Discord
    snowflakes are regularly bigger than that. Python has no such limit,
    so storing/returning the digits as a string sidesteps the whole
    problem everywhere this value travels. Every place that actually calls
    a discord.py method with one of these (guild.get_channel(),
    guild.get_role(), etc.) wraps it in int(...) at that call site.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw.isdigit():
        return None
    return raw


def env_first(env_key, dashboard_value, default=""):
    """Live env-var override for a piece of freeform text (welcome/level-up
    message templates, currently). Unlike every ID/channel setting in this
    schema - which only ever reads its .env var ONCE, as the default the
    first time a guild's config is created, after which the dashboard is
    the source of truth - this is checked fresh on every call. That means
    setting the env var on the host always wins over whatever's saved in
    the dashboard for that guild, and clearing the env var (leaving it
    blank) immediately falls back to the dashboard's value again, no
    restart needed either way. Falls back to `default` if neither is set.
    """
    env_val = (os.getenv(env_key) or "").strip()
    if env_val:
        return env_val
    if dashboard_value:
        return dashboard_value
    return default


def load_cfg():
    return store.load(CFG_FILE)


def save_cfg(cfg):
    store.save(CFG_FILE, cfg)


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
    g.setdefault("staff_role_id", _env_id("STAFF_ROLE_ID"))

    # ---- ticket ----
    g.setdefault("ticket_category_id", _env_id("TICKET_CATEGORY_ID"))
    g.setdefault("ticket_ping_role_id", _env_id("TICKET_PING_ROLE_ID"))

    # ---- per-game channel locking ----
    # {command_name: channel_id_str}. Empty until a channel is auto-detected
    # by name match or set explicitly via the dashboard/.env - see
    # cog_utils.resolve_game_channel for the actual resolution order.
    g.setdefault("game_channels", {})

    # ---- custom per-server "env" ----
    # freeform {KEY: "value"} strings, editable from the dashboard's Custom
    # settings tab. cog_utils.get_custom_setting() checks this dict first
    # and falls back to the real os.environ value of the same key, so any
    # env var in this project can be overridden per-server without anyone
    # needing shell/host access.
    g.setdefault("custom_env", {})

    # ---- welcome messages ----
    # channel follows the same one-time-default pattern as every other
    # channel ID in this schema (env var seeds it once, dashboard owns it
    # after that). the MESSAGE TEXT is different on purpose - see
    # env_first() above - so ops can force a message from the host without
    # a dashboard trip, but everyday editing happens in the dashboard.
    g.setdefault("welcome_channel_id", _env_id("WELCOME_CHANNEL_ID"))
    g.setdefault("welcome_enabled", _env_id("WELCOME_CHANNEL_ID") is not None)
    g.setdefault("welcome_message", None)  # dashboard template; WELCOME_MESSAGE env wins live if set

    # ---- leveling ----
    # text-message XP with a per-user cooldown (see cogs/leveling.py),
    # level-up announcements, and optional role rewards per level.
    g.setdefault("leveling_enabled", True)
    g.setdefault("level_channel_id", _env_id("LEVEL_CHANNEL_ID"))
    g.setdefault("level_message", None)  # dashboard template; LEVEL_UP_MESSAGE env wins live if set
    g.setdefault("level_roles", {})  # {"<level>": role_id} - awarded (stacking, never removed) on level-up

    # ---- rebirth (prestige reset) ----
    # {"<rebirth count>": role_id} - same stacking behavior as level_roles:
    # reaching that many rebirths grants every configured role at or below
    # the count the member doesn't already have, nothing is ever removed.
    g.setdefault("rebirth_roles", {})

    # ---- live member-count voice channel (cogs/server_stats.py) ----
    # renames a voice channel to show how many non-bot members are in the
    # server, e.g. "Members: 42". channel follows the usual one-time-env-
    # default pattern; the NAME TEMPLATE uses env_first() like the welcome/
    # level-up messages do.
    g.setdefault("member_count_channel_id", _env_id("MEMBER_COUNT_CHANNEL_ID"))
    g.setdefault("member_count_enabled", _env_id("MEMBER_COUNT_CHANNEL_ID") is not None)
    g.setdefault("member_count_template", None)  # dashboard template; MEMBER_COUNT_TEMPLATE env wins live if set

    # ---- logging (message / mod / report / withdraw / deposit / ticket) ----
    for log_type, env_var in LOG_TYPES.items():
        env_channel = _env_id(env_var)
        g.setdefault(f"log_{log_type}_channel", env_channel)
        g.setdefault(f"log_{log_type}_enabled", env_channel is not None)

    return g


def known_guild_ids(cfg=None):
    return set((cfg if cfg is not None else load_cfg()).keys())