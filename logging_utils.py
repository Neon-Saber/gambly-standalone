"""
Discord-facing helpers that sit on top of config_schema. This is what
cogs/bot.py actually call - config_schema.py itself never touches
discord.py so the Flask dashboard process doesn't need to import it.
"""
import discord

import config_schema as cfgschema


def _guild_cfg(guild_id, name=None):
    cfg = cfgschema.load_cfg()
    return cfgschema.ensure_guild(cfg, guild_id, name)


def resolve_log_channel(guild, log_type):
    """Return the discord channel object configured for `log_type` in this
    guild, or None if that log type is disabled, unconfigured, or the
    channel can't be found. Public (unlike _guild_cfg) because a couple of
    callers - the ticket transcript upload, notably - need the channel
    object itself rather than just "did send_log succeed"."""
    if guild is None:
        return None
    g = _guild_cfg(guild.id, guild.name)
    if not g.get(f"log_{log_type}_enabled"):
        return None
    channel_id = g.get(f"log_{log_type}_channel")
    if not channel_id:
        return None
    return guild.get_channel(int(channel_id))


async def send_log(bot, guild, log_type, embed):
    """Send `embed` to whatever channel is configured for `log_type` in
    this guild, IF that log type is turned on and has a channel set.
    Never raises - a broken/missing log config should never take down
    whatever feature triggered the log attempt. Returns True/False for
    whether it actually sent, in case a caller wants to tell the user
    "heads up, staff won't see this" (e.g. the report command).
    """
    channel = resolve_log_channel(guild, log_type)
    if channel is None:
        if guild is not None:
            g = _guild_cfg(guild.id)
            if g.get(f"log_{log_type}_enabled") and g.get(f"log_{log_type}_channel"):
                # it WAS configured, just couldn't be found - worth a console note
                print(f"[logging] {log_type} log channel ({g.get(f'log_{log_type}_channel')}) not found in "
                      f"'{guild.name}' - was it deleted? check the dashboard's Moderation & Logging tab.")
        return False
    try:
        await channel.send(embed=embed)
        return True
    except discord.Forbidden:
        print(f"[logging] no permission to send in the {log_type} log channel in '{guild.name}' "
              f"- check the bot's permissions in that channel.")
    except discord.HTTPException as e:
        print(f"[logging] failed to send {log_type} log in '{guild.name}': {e}")
    return False


def get_staff_role_id(guild_id):
    return _guild_cfg(guild_id).get("staff_role_id")


def is_staff(member: discord.Member) -> bool:
    """Administrators always count as staff. Otherwise, checks the
    per-guild staff_role_id (configurable from the dashboard, defaults
    from STAFF_ROLE_ID in .env the first time this guild's config is
    created)."""
    if member.guild_permissions.administrator:
        return True
    role_id = get_staff_role_id(member.guild.id)
    if role_id and any(r.id == int(role_id) for r in member.roles):
        return True
    return False


def get_ticket_settings(guild_id):
    g = _guild_cfg(guild_id)
    return {
        "category_id": g.get("ticket_category_id"),
        "ping_role_id": g.get("ticket_ping_role_id"),
    }
