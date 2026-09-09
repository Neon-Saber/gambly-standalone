"""
Moderation commands - kick, ban, timeout (mute), warnings, purge, slowmode,
channel lock/unlock. Every action logs through logging_utils.send_log() to
whichever channel the "mod" log type is configured to in config.json - set
initially from MOD_LOG_CHANNEL_ID in .env, editable anytime from the
dashboard's Moderation & Logging tab without a restart.

Every command below is available BOTH as a slash command and as a prefix
command (e.g. `/kick` and `!kick`, or whatever prefix this server has set
in the dashboard's Server settings tab - same `command_prefix=get_prefix`
the casino commands already use, no extra wiring needed). Each pair shares
one `_do_*` method so the actual logic only exists once; the two thin
command wrappers just adapt slash vs. prefix response mechanics via
cog_utils.

Permission model:
- Server admins can always use every command here.
- Anyone with the guild's configured staff role (dashboard: Moderation &
  Logging -> Staff role; .env default: STAFF_ROLE_ID) can use everything
  except ban/unban, which need the native "Ban Members" Discord permission
  specifically (administrators have this by default).

Note: the prefix version of /ban doesn't take `delete_days` (awkward to
parse alongside a free-text reason from a plain message) - it always
leaves message history alone. Use the slash command if you need that.
"""
import json
import time
from pathlib import Path
from datetime import timedelta, datetime, timezone

import discord
from discord.ext import commands
from discord import Option

import logging_utils
import embeds
import cog_utils as cu

WARNINGS_FILE = Path(__file__).parent.parent / "warnings.json"
MAX_TIMEOUT_SECONDS = 28 * 86400  # Discord's own timeout cap
MAX_SLOWMODE_SECONDS = 21600  # Discord's own slowmode cap (6h)


def load_warnings():
    if not WARNINGS_FILE.exists():
        return {}
    try:
        with open(WARNINGS_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_warnings(data):
    with open(WARNINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def staff_check():
    async def predicate(ctx):
        if not isinstance(ctx.author, discord.Member):
            return False
        if logging_utils.is_staff(ctx.author):
            return True
        raise commands.MissingPermissions(["manage_guild"])
    return commands.check(predicate)


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def log(self, guild, title, member, reason, color=embeds.COLOR_INFO, extra=None, moderator=None):
        embed = embeds.mod_action_embed(title, member, moderator, reason, color, extra)
        await logging_utils.send_log(self.bot, guild, "mod", embed)

    async def dm_notice(self, member, guild_name, action, reason):
        try:
            embed = discord.Embed(
                title=f"you were {action} in {guild_name}",
                description=f"**reason:** {reason or 'no reason given'}",
                color=embeds.COLOR_SEVERE,
            )
            await member.send(embed=embed)
        except discord.HTTPException:
            pass  # DMs closed, oh well

    # ---------------------------------------------------------------- kick --
    async def _do_kick(self, ctx, member, reason):
        if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await cu.respond(ctx, "can't kick someone with an equal/higher role than you", ephemeral=True)
        await self.dm_notice(member, ctx.guild.name, "kicked", reason)
        await ctx.guild.kick(member, reason=f"{ctx.author} ({ctx.author.id}): {reason or 'no reason'}")
        await self.log(ctx.guild, "member kicked", member, reason, color=embeds.COLOR_SEVERE, moderator=ctx.author)
        await cu.respond(ctx, f"👢 kicked **{member}**" + (f" — {reason}" if reason else ""))

    @commands.slash_command(name="kick", description="kick a member from the server")
    @staff_check()
    async def kick(self, ctx, member: Option(discord.Member, "who"),
                   reason: Option(str, "why", required=False) = None):
        await self._do_kick(ctx, member, reason)

    @commands.command(name="kick")
    @staff_check()
    async def kick_cmd(self, ctx, member: discord.Member, *, reason: str = None):
        await self._do_kick(ctx, member, reason)

    # ----------------------------------------------------------------- ban --
    async def _do_ban(self, ctx, user, reason, delete_days=0):
        member = None
        try:
            member = await commands.MemberConverter().convert(ctx, user)
        except commands.BadArgument:
            pass

        if member is not None:
            if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
                return await cu.respond(ctx, "can't ban someone with an equal/higher role than you", ephemeral=True)
            await self.dm_notice(member, ctx.guild.name, "banned", reason)
            await ctx.guild.ban(member, reason=f"{ctx.author} ({ctx.author.id}): {reason or 'no reason'}",
                                delete_message_days=delete_days)
            target_display = member
        else:
            try:
                user_id = int(user.strip("<@!>"))
            except ValueError:
                return await cu.respond(ctx, "couldn't find that member and it's not a valid user ID either", ephemeral=True)
            await ctx.guild.ban(discord.Object(id=user_id),
                                reason=f"{ctx.author} ({ctx.author.id}): {reason or 'no reason'}",
                                delete_message_days=delete_days)
            target_display = await self.bot.fetch_user(user_id)

        await self.log(ctx.guild, "member banned", target_display, reason, color=embeds.COLOR_SEVERE, moderator=ctx.author)
        await cu.respond(ctx, f"🔨 banned **{target_display}**" + (f" — {reason}" if reason else ""))

    @commands.slash_command(name="ban", description="ban a member (or a raw user ID) from the server")
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx, user: Option(str, "member mention, username, or raw user ID"),
                  reason: Option(str, "why", required=False) = None,
                  delete_days: Option(int, "days of message history to delete", min_value=0, max_value=7, required=False) = 0):
        await self._do_ban(ctx, user, reason, delete_days)

    @commands.command(name="ban")
    @commands.has_permissions(ban_members=True)
    async def ban_cmd(self, ctx, user: str, *, reason: str = None):
        await self._do_ban(ctx, user, reason, delete_days=0)

    async def _do_unban(self, ctx, user_id):
        try:
            uid = int(str(user_id).strip("<@!>"))
        except ValueError:
            return await cu.respond(ctx, "that doesn't look like a valid user ID", ephemeral=True)
        try:
            user = await self.bot.fetch_user(uid)
            await ctx.guild.unban(user, reason=f"unbanned by {ctx.author} ({ctx.author.id})")
        except discord.NotFound:
            return await cu.respond(ctx, "that user isn't banned here", ephemeral=True)
        await self.log(ctx.guild, "member unbanned", user, None, color=embeds.COLOR_INFO, moderator=ctx.author)
        await cu.respond(ctx, f"✅ unbanned **{user}**")

    @commands.slash_command(name="unban", description="lift a ban by user ID")
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx, user_id: Option(str, "the banned user's ID")):
        await self._do_unban(ctx, user_id)

    @commands.command(name="unban")
    @commands.has_permissions(ban_members=True)
    async def unban_cmd(self, ctx, user_id: str):
        await self._do_unban(ctx, user_id)

    # ------------------------------------------------------------- timeout --
    async def _do_mute(self, ctx, member, seconds, reason):
        if member.top_role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
            return await cu.respond(ctx, "can't mute someone with an equal/higher role than you", ephemeral=True)
        until = discord.utils.utcnow() + timedelta(seconds=seconds)
        await member.timeout(until, reason=f"{ctx.author} ({ctx.author.id}): {reason or 'no reason'}")
        human = cu.format_duration(seconds)
        await self.log(ctx.guild, "member muted", member, reason, color=embeds.COLOR_WARN,
                        extra={"duration": human}, moderator=ctx.author)
        await cu.respond(ctx, f"🔇 muted **{member}** for {human}" + (f" — {reason}" if reason else ""))

    @commands.slash_command(name="mute", description="timeout a member so they can't send messages/talk for a while")
    @staff_check()
    async def mute(self, ctx, member: Option(discord.Member, "who"),
                   duration: Option(str, "how long - e.g. 10m, 2h, 1d (a bare number means minutes)"),
                   reason: Option(str, "why", required=False) = None):
        try:
            seconds = cu.parse_duration(duration, default_unit="m")
        except ValueError as e:
            return await cu.respond(ctx, str(e), ephemeral=True)
        if seconds > MAX_TIMEOUT_SECONDS:
            return await cu.respond(ctx, "that's longer than Discord's 28-day timeout limit", ephemeral=True)
        await self._do_mute(ctx, member, seconds, reason)

    @commands.command(name="mute")
    @staff_check()
    async def mute_cmd(self, ctx, member: discord.Member, duration: str, *, reason: str = None):
        try:
            seconds = cu.parse_duration(duration, default_unit="m")
        except ValueError as e:
            return await cu.respond(ctx, str(e), ephemeral=True)
        if seconds > MAX_TIMEOUT_SECONDS:
            return await cu.respond(ctx, "that's longer than Discord's 28-day timeout limit", ephemeral=True)
        await self._do_mute(ctx, member, seconds, reason)

    async def _do_unmute(self, ctx, member):
        await member.timeout(None, reason=f"unmuted by {ctx.author} ({ctx.author.id})")
        await self.log(ctx.guild, "member unmuted", member, None, color=embeds.COLOR_INFO, moderator=ctx.author)
        await cu.respond(ctx, f"🔊 unmuted **{member}**")

    @commands.slash_command(name="unmute", description="remove an active timeout from a member")
    @staff_check()
    async def unmute(self, ctx, member: Option(discord.Member, "who")):
        await self._do_unmute(ctx, member)

    @commands.command(name="unmute")
    @staff_check()
    async def unmute_cmd(self, ctx, member: discord.Member):
        await self._do_unmute(ctx, member)

    # -------------------------------------------------------------- warns --
    async def _do_warn(self, ctx, member, reason):
        data = load_warnings()
        gid, uid = str(ctx.guild.id), str(member.id)
        data.setdefault(gid, {}).setdefault(uid, [])
        data[gid][uid].append({"reason": reason, "by": ctx.author.id, "at": time.time()})
        save_warnings(data)
        count = len(data[gid][uid])
        await self.dm_notice(member, ctx.guild.name, "warned", reason)
        await self.log(ctx.guild, "member warned", member, reason, color=embeds.COLOR_WARN,
                        extra={"total warnings": str(count)}, moderator=ctx.author)
        await cu.respond(ctx, f"⚠️ warned **{member}** (warning #{count}) — {reason}")

    @commands.slash_command(name="warn", description="log a warning against a member")
    @staff_check()
    async def warn(self, ctx, member: Option(discord.Member, "who"), reason: Option(str, "why")):
        await self._do_warn(ctx, member, reason)

    @commands.command(name="warn")
    @staff_check()
    async def warn_cmd(self, ctx, member: discord.Member, *, reason: str):
        await self._do_warn(ctx, member, reason)

    async def _do_warnings(self, ctx, member):
        data = load_warnings()
        entries = data.get(str(ctx.guild.id), {}).get(str(member.id), [])
        if not entries:
            return await cu.respond(ctx, f"**{member}** has no warnings on file", ephemeral=True)
        embed = discord.Embed(title=f"warnings — {member}", color=embeds.COLOR_WARN)
        for i, w in enumerate(entries[-15:], 1):
            when = discord.utils.format_dt(datetime.fromtimestamp(w["at"], tz=timezone.utc), "R")
            embed.add_field(name=f"#{i} — {when}", value=f"{w['reason']} (by <@{w['by']}>)", inline=False)
        await cu.respond(ctx, embed=embed, ephemeral=True)

    @commands.slash_command(name="warnings", description="see a member's warning history")
    @staff_check()
    async def warnings_cmd(self, ctx, member: Option(discord.Member, "who")):
        await self._do_warnings(ctx, member)

    @commands.command(name="warnings")
    @staff_check()
    async def warnings_prefix_cmd(self, ctx, member: discord.Member):
        await self._do_warnings(ctx, member)

    async def _do_clearwarnings(self, ctx, member):
        data = load_warnings()
        gid, uid = str(ctx.guild.id), str(member.id)
        had_any = bool(data.get(gid, {}).get(uid))
        data.setdefault(gid, {})[uid] = []
        save_warnings(data)
        await self.log(ctx.guild, "warnings cleared", member, None, color=embeds.COLOR_INFO, moderator=ctx.author)
        await cu.respond(ctx, f"🧹 cleared warnings for **{member}**" if had_any else f"**{member}** had no warnings")

    @commands.slash_command(name="clearwarnings", description="wipe a member's warning history")
    @commands.has_permissions(manage_guild=True)
    async def clearwarnings(self, ctx, member: Option(discord.Member, "who")):
        await self._do_clearwarnings(ctx, member)

    @commands.command(name="clearwarnings")
    @commands.has_permissions(manage_guild=True)
    async def clearwarnings_cmd(self, ctx, member: discord.Member):
        await self._do_clearwarnings(ctx, member)

    # -------------------------------------------------------------- purge --
    async def _do_purge(self, ctx, amount, member):
        await cu.maybe_defer(ctx, ephemeral=True)
        check = (lambda m: m.author.id == member.id) if member else None
        deleted = await ctx.channel.purge(limit=amount, check=check)
        detail = f"{len(deleted)} message(s) in #{ctx.channel.name}" + (f" from {member}" if member else "")
        await self.log(ctx.guild, "messages purged", ctx.author, detail, color=embeds.COLOR_INFO, moderator=ctx.author)
        await cu.followup(ctx, f"🧹 deleted {len(deleted)} message(s)", ephemeral=True)

    @commands.slash_command(name="purge", description="bulk-delete recent messages in this channel")
    @staff_check()
    async def purge(self, ctx, amount: Option(int, "how many messages", min_value=1, max_value=100),
                    member: Option(discord.Member, "only delete messages from this member", required=False) = None):
        await self._do_purge(ctx, amount, member)

    @commands.command(name="purge")
    @staff_check()
    async def purge_cmd(self, ctx, amount: int, member: discord.Member = None):
        if amount < 1 or amount > 100:
            return await cu.respond(ctx, "amount must be between 1 and 100", ephemeral=True)
        # the invoking !purge message itself counts as one to clean up too
        await self._do_purge(ctx, amount + 1, member)

    # ----------------------------------------------------------- slowmode --
    async def _do_slowmode(self, ctx, seconds):
        await ctx.channel.edit(slowmode_delay=seconds)
        await self.log(ctx.guild, "slowmode changed", ctx.author, f"#{ctx.channel.name} -> {seconds}s",
                        color=embeds.COLOR_INFO, moderator=ctx.author)
        await cu.respond(ctx, f"🐌 slowmode set to {seconds}s in {ctx.channel.mention}" if seconds
                          else f"slowmode disabled in {ctx.channel.mention}")

    @commands.slash_command(name="slowmode", description="set this channel's slowmode delay")
    @staff_check()
    async def slowmode(self, ctx, seconds: Option(int, "seconds between messages, 0 to disable", min_value=0, max_value=21600)):
        await self._do_slowmode(ctx, seconds)

    @commands.command(name="slowmode")
    @staff_check()
    async def slowmode_cmd(self, ctx, seconds: int):
        if seconds < 0 or seconds > 21600:
            return await cu.respond(ctx, "seconds must be between 0 and 21600 (6h)", ephemeral=True)
        await self._do_slowmode(ctx, seconds)

    # --------------------------------------------------------- lock/unlock --
    async def _do_lock(self, ctx, reason):
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite,
                                           reason=f"locked by {ctx.author}")
        await self.log(ctx.guild, "channel locked", ctx.author, reason, color=embeds.COLOR_WARN,
                        extra={"channel": ctx.channel.mention}, moderator=ctx.author)
        await cu.respond(ctx, f"🔒 locked {ctx.channel.mention}" + (f" — {reason}" if reason else ""))

    @commands.slash_command(name="lock", description="stop @everyone sending messages in this channel")
    @staff_check()
    async def lock(self, ctx, reason: Option(str, "why", required=False) = None):
        await self._do_lock(ctx, reason)

    @commands.command(name="lock")
    @staff_check()
    async def lock_cmd(self, ctx, *, reason: str = None):
        await self._do_lock(ctx, reason)

    async def _do_unlock(self, ctx):
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite,
                                           reason=f"unlocked by {ctx.author}")
        await self.log(ctx.guild, "channel unlocked", ctx.author, None, color=embeds.COLOR_INFO,
                        extra={"channel": ctx.channel.mention}, moderator=ctx.author)
        await cu.respond(ctx, f"🔓 unlocked {ctx.channel.mention}")

    @commands.slash_command(name="unlock", description="let @everyone send messages in this channel again")
    @staff_check()
    async def unlock(self, ctx):
        await self._do_unlock(ctx)

    @commands.command(name="unlock")
    @staff_check()
    async def unlock_cmd(self, ctx):
        await self._do_unlock(ctx)

    # note: MissingPermissions / generic errors are all handled centrally by
    # bot.py's on_application_command_error (slash) and would need
    # on_command_error (prefix) if bot.py adds one later - it doesn't
    # currently define one, so prefix permission errors currently just
    # print to console via discord.py's default behavior rather than
    # replying in-channel. Slash commands (the primary interface) are
    # unaffected either way.


def setup(bot):
    bot.add_cog(Moderation(bot))