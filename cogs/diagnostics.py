"""
!diag / !diagnostics / !healthcheck (and /diag) - a health check for this
server's dashboard-configured features, staff only.

Scope, honestly: this is NOT a system that autonomously finds and patches
arbitrary bugs in the bot's code - that's not something that can be
bolted onto a running bot, and pretending otherwise would be a lie. What
it actually does is walk every dashboard-configurable feature (welcome,
leveling, member count, staff role, each log type, tickets) and report,
in plain language, exactly what's wrong with it - a disabled toggle, a
missing channel, a channel that got deleted, a permission the bot
doesn't have - instead of the silent "nothing happens, check console"
failure mode this project used to have everywhere.

One genuine auto-fix is included: if welcome/leveling/member-count has no
channel configured yet, it tries the exact same name-matching auto-detect
cog_utils.resolve_game_channel already uses for the casino game channels,
and saves the result if it finds one - the single most common failure
mode in practice ("I picked it in the dashboard" but the picker never
actually saved anything, see the JS-precision and channel-list-failed
bugs this project has hit before).
"""
import discord
from discord.ext import commands

import config_schema as cfgschema
import cog_utils as cu
import store

_WELCOME_ALIASES = ["welcome", "welcomes", "greet", "greetings"]
_LEVEL_ALIASES = ["level", "levels", "levelup", "level-up", "rank", "ranks"]
_MEMBER_COUNT_ALIASES = ["member", "members", "count", "stats"]

_EXPECTED_EXTENSIONS = [
    "cogs.moderation", "cogs.tickets", "cogs.reports", "cogs.logging_events",
    "cogs.leveling", "cogs.welcome", "cogs.server_stats",
]


class Diagnostics(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------- checks --
    async def _check_send_channel(self, guild, g_cfg, enabled_key, channel_key, aliases, kind="text"):
        """Shared logic for welcome/level/member-count: auto-detect a
        missing channel by name, then report enabled/channel/permission
        status. Returns (lines: list[str], auto_fixed: bool)."""
        lines = []
        auto_fixed = False
        channel_id = g_cfg.get(channel_key)

        if not channel_id:
            found, changed = cu.resolve_named_channel(guild, g_cfg, channel_key, aliases, kind=kind)
            if changed:
                channel_id = found
                auto_fixed = True
                lines.append(f"🔧 no channel was set - auto-detected and saved <#{found}>")

        if not g_cfg.get(enabled_key):
            lines.append("⏸️ disabled in the dashboard")
            return lines, auto_fixed
        if not channel_id:
            lines.append("⚠️ enabled, but no channel is set (and nothing matched auto-detect by name)")
            return lines, auto_fixed

        channel, err = await cu.resolve_channel(guild, channel_id)
        if err:
            lines.append(f"❌ {err}")
            return lines, auto_fixed

        me = guild.me
        perms = channel.permissions_for(me)
        if kind == "voice":
            if not perms.manage_channels:
                lines.append(f"❌ {channel.mention} - bot is missing **Manage Channel** (needed to rename it)")
                return lines, auto_fixed
        else:
            missing = [p.replace("_", " ") for p in ("view_channel", "send_messages", "embed_links")
                       if not getattr(perms, p)]
            if missing:
                lines.append(f"❌ {channel.mention} - bot is missing: {', '.join(missing)}")
                return lines, auto_fixed

        lines.append(f"✅ {channel.mention} - looks good")
        return lines, auto_fixed

    async def _run(self, ctx):
        guild = ctx.guild
        if guild is None:
            return await cu.respond(ctx, "server only", ephemeral=True)

        all_cfg = cfgschema.load_cfg()
        g_cfg = cfgschema.ensure_guild(all_cfg, guild.id, guild.name)
        changed_anything = False

        embed = discord.Embed(title=f"🩺 diagnostics — {guild.name}", color=discord.Color.blurple())

        # ---- storage backend ----
        store_lines = []
        try:
            # writes/reads a small fixed key (not guild-specific, so repeat
            # runs don't leave a pile of test keys behind) - this exercises
            # the exact same read/write path every real feature uses.
            store.save("diag_healthcheck", {"ok": True})
            store.load("diag_healthcheck")
            store_lines.append("✅ Upstash reachable (read/write round-trip OK)" if store._enabled()
                                else "⚠️ LOCAL FILES ONLY - UPSTASH_REDIS_REST_URL/TOKEN aren't both "
                                     "set on THIS machine, so saves here won't reach other machines")
        except Exception as e:
            store_lines.append(f"❌ storage round-trip failed: {e}")
        embed.add_field(name="Storage", value="\n".join(store_lines), inline=False)

        # ---- welcome ----
        lines, fixed = await self._check_send_channel(guild, g_cfg, "welcome_enabled", "welcome_channel_id", _WELCOME_ALIASES)
        changed_anything = changed_anything or fixed
        embed.add_field(name="Welcome messages", value="\n".join(lines), inline=False)

        # ---- leveling ----
        lines, fixed = await self._check_send_channel(guild, g_cfg, "leveling_enabled", "level_channel_id", _LEVEL_ALIASES)
        changed_anything = changed_anything or fixed
        embed.add_field(name="Level-up announcements", value="\n".join(lines), inline=False)

        # ---- member count (voice) ----
        lines, fixed = await self._check_send_channel(guild, g_cfg, "member_count_enabled",
                                                        "member_count_channel_id", _MEMBER_COUNT_ALIASES, kind="voice")
        changed_anything = changed_anything or fixed
        embed.add_field(name="Live member count", value="\n".join(lines), inline=False)

        # ---- staff role ----
        staff_role_id = g_cfg.get("staff_role_id")
        if not staff_role_id:
            staff_line = "⚠️ no staff role set - only server Administrators can use mod/ticket-staff commands"
        else:
            role = guild.get_role(int(staff_role_id))
            staff_line = f"✅ {role.mention}" if role else f"❌ role `{staff_role_id}` no longer exists"
        embed.add_field(name="Staff role", value=staff_line, inline=False)

        # ---- logging channels ----
        log_lines = []
        for log_type in cfgschema.LOG_TYPES:
            enabled = g_cfg.get(f"log_{log_type}_enabled")
            channel_id = g_cfg.get(f"log_{log_type}_channel")
            if not enabled:
                log_lines.append(f"⏸️ **{log_type}**: disabled")
                continue
            if not channel_id:
                log_lines.append(f"⚠️ **{log_type}**: enabled, no channel set")
                continue
            channel, err = await cu.resolve_channel(guild, channel_id)
            if err:
                log_lines.append(f"❌ **{log_type}**: {err}")
                continue
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                log_lines.append(f"❌ **{log_type}**: {channel.mention} - missing permissions")
                continue
            log_lines.append(f"✅ **{log_type}**: {channel.mention}")
        embed.add_field(name="Logging", value="\n".join(log_lines) or "nothing configured", inline=False)

        # ---- tickets ----
        cat_id = g_cfg.get("ticket_category_id")
        if not cat_id:
            ticket_line = "⚠️ no ticket category set - `/ticket-panel` has nowhere to create channels"
        else:
            cat = guild.get_channel(int(cat_id))
            ticket_line = f"✅ {cat.name}" if cat else f"❌ category `{cat_id}` no longer exists"
        embed.add_field(name="Tickets", value=ticket_line, inline=False)

        # ---- cogs loaded ----
        cog_lines = [f"{'✅' if ext in self.bot.extensions else '❌'} {ext}" for ext in _EXPECTED_EXTENSIONS]
        embed.add_field(name="Cogs loaded", value="\n".join(cog_lines), inline=False)

        if changed_anything:
            cfgschema.save_cfg(all_cfg)
            embed.set_footer(text="🔧 auto-detected channel(s) saved to config - re-run to confirm")

        await cu.respond(ctx, embed=embed, ephemeral=True)

    @commands.slash_command(name="diag", description="health-check this server's dashboard-configured features")
    @cu.staff_check()
    async def diag_slash(self, ctx):
        await cu.maybe_defer(ctx, ephemeral=True)
        await self._run(ctx)

    @commands.command(name="diag", aliases=["diagnostics", "healthcheck"])
    @cu.staff_check()
    async def diag_prefix(self, ctx):
        await self._run(ctx)


def setup(bot):
    bot.add_cog(Diagnostics(bot))
