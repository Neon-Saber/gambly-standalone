"""
Text-message leveling, like any other leveling bot (MEE6/Tatsu/etc.):
sending messages earns XP (with a per-user cooldown so spamming doesn't
inflate it), leveling up posts an announcement embed, and levels can be
tied to roles that get handed out automatically.

Guild-only feature on purpose - "who's active in this server" doesn't
mean anything in a DM. Skips bots and the game-channel-locked commands'
own spam entirely (any message counts, including ones that also happen
to be commands).

Everything here is dashboard-first (Server settings -> Leveling &
Welcome tab): enabled/disabled, the announce channel, the message
template, and the level -> role map. The channel follows this project's
usual one-time-env-default-then-dashboard pattern; the message template
uses config_schema.env_first() instead, so an env var can force it live
if set, otherwise the dashboard's saved template is used.
"""
import random
import time
from pathlib import Path

import discord
from discord.ext import commands
from discord import Option

import config_schema as cfgschema
import cog_utils as cu
import store

LEVELS_FILE = Path(__file__).parent.parent / "levels.json"
XP_MIN, XP_MAX = 15, 25
XP_COOLDOWN = 60  # seconds between XP-earning messages, per user

DEFAULT_LEVEL_MESSAGE = "🎉 {user} just reached **level {level}**!"


def load_levels():
    return store.load(LEVELS_FILE)


def save_levels(data):
    store.save(LEVELS_FILE, data)


def xp_needed_for(level):
    """XP required to climb from `level` to `level + 1`. Gets a bit
    steeper each level, same shape as most leveling bots' curves."""
    return 100 + level * 50


def ensure_entry(guild_data, member):
    uid = str(member.id)
    if uid not in guild_data:
        guild_data[uid] = {"name": member.display_name, "xp": 0, "level": 0, "total_xp": 0, "last_gain": 0}
    else:
        guild_data[uid]["name"] = member.display_name
        guild_data[uid].setdefault("total_xp", guild_data[uid].get("xp", 0))
    return guild_data[uid]


def render_template(template, member, guild, level=None):
    text = template
    text = text.replace("{user}", member.mention)
    text = text.replace("{username}", str(member.display_name))
    text = text.replace("{server}", guild.name)
    text = text.replace("{membercount}", str(guild.member_count))
    if level is not None:
        text = text.replace("{level}", str(level))
    return text


class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return

        all_cfg = cfgschema.load_cfg()
        g_cfg = cfgschema.ensure_guild(all_cfg, message.guild.id, message.guild.name)
        if not g_cfg.get("leveling_enabled", True):
            return

        levels = load_levels()
        gid = str(message.guild.id)
        guild_levels = levels.setdefault(gid, {})
        entry = ensure_entry(guild_levels, message.author)

        now = time.time()
        if now - entry.get("last_gain", 0) < XP_COOLDOWN:
            return
        entry["last_gain"] = now

        gained = random.randint(XP_MIN, XP_MAX)
        entry["xp"] += gained
        entry["total_xp"] = entry.get("total_xp", 0) + gained

        leveled_up = False
        while entry["xp"] >= xp_needed_for(entry["level"]):
            entry["xp"] -= xp_needed_for(entry["level"])
            entry["level"] += 1
            leveled_up = True

        save_levels(levels)

        if leveled_up:
            await self._announce_level_up(message.guild, message.author, entry["level"], g_cfg)
            await self._grant_level_roles(message.guild, message.author, entry["level"], g_cfg)

    async def _announce_level_up(self, guild, member, level, g_cfg):
        channel_id = g_cfg.get("level_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return  # not configured (or the saved channel got deleted) - level still recorded either way
        template = cfgschema.env_first("LEVEL_UP_MESSAGE", g_cfg.get("level_message"), DEFAULT_LEVEL_MESSAGE)
        text = render_template(template, member, guild, level=level)
        embed = discord.Embed(description=text, color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            print(f"[leveling] no permission to post the level-up announcement in '{guild.name}'")
        except discord.HTTPException as e:
            print(f"[leveling] failed to post level-up announcement in '{guild.name}': {e}")

    async def _grant_level_roles(self, guild, member, level, g_cfg):
        # stacking rewards: hand out every configured role at or below the
        # level just reached that the member doesn't already have. never
        # removes anything - if you want a "latest tier only" look, just
        # don't reuse a member's old tier role for a new one.
        level_roles = g_cfg.get("level_roles", {})
        if not level_roles:
            return
        to_add = []
        for lvl_str, role_id in level_roles.items():
            try:
                if int(lvl_str) > level:
                    continue
            except (TypeError, ValueError):
                continue
            role = guild.get_role(int(role_id)) if role_id else None
            if role and role not in member.roles:
                to_add.append(role)
        if to_add:
            try:
                await member.add_roles(*to_add, reason=f"reached level {level}")
            except discord.Forbidden:
                print(f"[leveling] missing permission to grant level roles in '{guild.name}' - "
                      f"check the bot's role position and Manage Roles permission")

    # -------------------------------------------------------------- rank --
    async def _do_rank(self, ctx, who):
        levels = load_levels()
        gid = str(ctx.guild.id)
        entry = levels.get(gid, {}).get(str(who.id))
        if not entry:
            return await cu.respond(ctx, f"{who.display_name} hasn't earned any XP here yet", ephemeral=True)
        needed = xp_needed_for(entry["level"])
        guild_levels = levels.get(gid, {})
        rank = sorted(guild_levels.values(), key=lambda e: -e.get("total_xp", 0))
        try:
            position = [e.get("total_xp", 0) for e in rank].index(entry.get("total_xp", 0)) + 1
        except ValueError:
            position = None
        desc = (
            f"level **{entry['level']}**\n"
            f"xp: {entry['xp']}/{needed} to next level\n"
            f"total xp: {entry.get('total_xp', 0):,}"
        )
        if position:
            desc += f"\nrank: #{position} of {len(rank)}"
        embed = discord.Embed(title=f"{who.display_name}'s rank", description=desc, color=discord.Color.blurple())
        embed.set_thumbnail(url=who.display_avatar.url)
        await cu.respond(ctx, embed=embed)

    @commands.slash_command(name="rank", description="see your (or someone's) level and xp")
    async def rank(self, ctx, user: Option(discord.Member, "who", required=False) = None):
        await self._do_rank(ctx, user or ctx.author)

    @commands.command(name="rank")
    async def rank_cmd(self, ctx, user: discord.Member = None):
        await self._do_rank(ctx, user or ctx.author)

    # ------------------------------------------------------------- levels --
    async def _do_levels(self, ctx):
        levels = load_levels()
        guild_levels = levels.get(str(ctx.guild.id), {})
        if not guild_levels:
            return await cu.respond(ctx, "nobody's earned any xp here yet")
        ranked = sorted(guild_levels.items(), key=lambda kv: -kv[1].get("total_xp", 0))[:10]
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for n, (uid, e) in enumerate(ranked):
            m = ctx.guild.get_member(int(uid))
            nm = m.display_name if m else e.get("name", "user " + uid)
            tag = medals[n] if n < 3 else f"{n + 1}."
            lines.append(f"{tag} {nm} - level {e['level']} ({e.get('total_xp', 0):,} xp)")
        await cu.respond(ctx, embed=discord.Embed(title="level leaderboard", description="\n".join(lines),
                                                   color=discord.Color.blurple()))

    @commands.slash_command(name="levels", description="see who's leveled up the most in this server")
    async def levels_cmd(self, ctx):
        await self._do_levels(ctx)

    @commands.command(name="levels", aliases=["levelboard"])
    async def levels_prefix_cmd(self, ctx):
        await self._do_levels(ctx)


def setup(bot):
    bot.add_cog(Leveling(bot))
