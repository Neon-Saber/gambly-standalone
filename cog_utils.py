"""
Tiny glue so every command in cogs/ can be written once and exposed both as
a slash command and a prefix command, mirroring how bot.py itself pairs
every economy/casino command (do_deposit() + a slash wrapper + a prefix
wrapper). A slash interaction uses ApplicationContext (ctx.respond /
ctx.defer / ctx.followup); a prefix message uses the plain Context
(ctx.send). These helpers pick the right one so the actual command logic
doesn't need to know or care which interface triggered it.
"""
import os
import re

import discord


async def respond(ctx, content=None, embed=None, ephemeral=False):
    if isinstance(ctx, discord.ApplicationContext):
        return await ctx.respond(content=content, embed=embed, ephemeral=ephemeral)
    if content is None and embed is None:
        return None
    return await ctx.send(content=content, embed=embed)


async def maybe_defer(ctx, ephemeral=False):
    """Slash commands can take >3s if they need to; prefix commands have no
    such limit and no defer() method at all, so this is a no-op for them."""
    if isinstance(ctx, discord.ApplicationContext):
        await ctx.defer(ephemeral=ephemeral)


async def followup(ctx, content=None, embed=None, ephemeral=False):
    if isinstance(ctx, discord.ApplicationContext):
        return await ctx.followup.send(content=content, embed=embed, ephemeral=ephemeral)
    return await ctx.send(content=content, embed=embed)


# ---------------------------------------------------------------- durations --
# lets every moderation command that takes a length of time accept plain
# numbers as well as "10s", "5m", "2h", "1d", or combos like "1h30m",
# instead of forcing one hardcoded unit.
_DURATION_TOKEN = re.compile(r"(\d+)\s*([smhdw]?)", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text, default_unit="s"):
    """Parse a duration string into a whole number of seconds.

    Accepts a bare number (uses default_unit), a single "10m"/"2h"/"1d"
    style value, or a combo like "1h30m". Raises ValueError with a
    human-readable message on anything that doesn't parse - callers should
    catch that and show it to the user rather than letting it propagate.
    """
    if text is None:
        raise ValueError("no duration given")
    cleaned = str(text).strip().lower().replace(" ", "")
    if not cleaned:
        raise ValueError("no duration given")

    total = 0
    matched_any = False
    for amount, unit in _DURATION_TOKEN.findall(cleaned):
        if not amount:
            continue
        matched_any = True
        unit = unit or default_unit
        seconds_per = _UNIT_SECONDS.get(unit)
        if seconds_per is None:
            raise ValueError(f"unknown time unit '{unit}' - use s/m/h/d/w")
        total += int(amount) * seconds_per

    if not matched_any or total <= 0:
        raise ValueError(f"couldn't read a duration from '{text}' - try something like 10m, 2h, or 1d")
    return total


def format_duration(seconds):
    """Inverse of parse_duration, for echoing back what got applied."""
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    parts = []
    for unit, secs in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= secs:
            val, seconds = divmod(seconds, secs)
            parts.append(f"{val}{unit}")
    return " ".join(parts)


# ------------------------------------------------------------ game channels --
# ties each casino game to one channel per server, so e.g. #crash only
# accepts /crash. resolution order per game, first match wins:
#   1. an explicit override set on the dashboard (or written here by step 3)
#   2. a per-game env var, e.g. CRASH_CHANNEL_ID
#   3. auto-detected by matching an existing text channel's name against
#      the aliases below, then cached into config.json so it only has to
#      scan the channel list once per server
# a game not in this dict is never channel-locked, no matter what.
GAME_CHANNEL_ALIASES = {
    "crash": ["crash"],
    "blackjack": ["blackjack"],
    "mines": ["mines"],
    "duel": ["duel", "duels"],
    "hilo": ["hilo", "highlow", "high-low", "high_low"],
    "roulette": ["roulette"],
    "allin": ["allin", "all-in", "roulette"],
    "coinflip": ["coinflip", "coin-flip"],
    "slots": ["slots"],
    "jackpot": ["slots", "jackpot"],
    "dice": ["dice"],
    "war": ["war"],
    "plinko": ["plinko"],
    "horserace": ["horserace", "horse-race", "horse_race", "races"],
    "wheel": ["wheel"],
    "keno": ["keno"],
    "baccarat": ["baccarat"],
    "rps": ["rps"],
    "ladder": ["ladder"],
}


def resolve_game_channel(guild, g_cfg, cmd_name):
    """Returns (channel_id_or_None, cfg_was_changed). Caller is responsible
    for saving config.json when cfg_was_changed is True - this function
    only mutates the in-memory g_cfg dict it's given."""
    aliases = GAME_CHANNEL_ALIASES.get(cmd_name)
    if aliases is None:
        return None, False

    game_channels = g_cfg.setdefault("game_channels", {})
    existing = game_channels.get(cmd_name)
    if existing:
        try:
            return int(existing), False
        except (TypeError, ValueError):
            pass  # fall through and re-resolve if it got corrupted somehow

    env_val = os.getenv(f"{cmd_name.upper()}_CHANNEL_ID", "").strip()
    if env_val.isdigit():
        game_channels[cmd_name] = env_val
        return int(env_val), True

    if guild is not None:
        for ch in getattr(guild, "text_channels", []):
            cname = ch.name.lower().replace("_", "-")
            if any(alias in cname for alias in aliases):
                game_channels[cmd_name] = str(ch.id)
                return ch.id, True

    return None, False


# -------------------------------------------------------------- custom env --
def get_custom_setting(g_cfg, key, default=None):
    """A per-guild override editable straight from the dashboard's Custom
    settings tab, acting like a server-specific .env - checked before the
    real environment variable of the same name, which stays the fallback
    default for every server that hasn't overridden it."""
    custom = g_cfg.get("custom_env") or {}
    if key in custom and custom[key] not in (None, ""):
        return custom[key]
    return os.getenv(key, default)