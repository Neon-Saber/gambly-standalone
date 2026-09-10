"""
Welcome messages, Welcomer-style: post an embed in a configured channel
whenever someone joins. Fully dashboard-driven (Server settings ->
Leveling & Welcome tab) - enabled/disabled, which channel, and the
message template all live in config.json, with the channel following
this project's usual one-time-env-default-then-dashboard pattern.

The message TEXT is the one exception - see config_schema.env_first():
setting WELCOME_MESSAGE in .env always wins live, checked fresh on every
join, and clearing it falls straight back to whatever's saved in the
dashboard. No restart needed for either.

Placeholders available in the template: {user} (mention), {username}
(display name, no ping), {server}, {membercount}.

`!welcome test` / `/welcome test` (staff only) fires the exact same
_send() path a real join uses, targeting you instead of a new member -
the fastest way to confirm the channel/permissions/template are actually
right without waiting for someone to join.
"""
import discord
from discord.ext import commands
from discord import SlashCommandGroup

import config_schema as cfgschema
import cog_utils as cu

DEFAULT_WELCOME_MESSAGE = "welcome {user} to **{server}**! you're member #{membercount} 🎉"

_NAME_ALIASES = ["welcome", "welcomes", "greet", "greetings"]


def render_template(template, member, guild):
    text = template
    text = text.replace("{user}", member.mention)
    text = text.replace("{username}", str(member.display_name))
    text = text.replace("{server}", guild.name)
    text = text.replace("{membercount}", str(guild.member_count))
    return text


def build_embed(member, guild, g_cfg):
    template = cfgschema.env_first("WELCOME_MESSAGE", g_cfg.get("welcome_message"), DEFAULT_WELCOME_MESSAGE)
    text = render_template(template, member, guild)
    embed = discord.Embed(description=text, color=discord.Color.gold(), timestamp=discord.utils.utcnow())
    embed.set_author(name=f"{member} joined", icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"member #{guild.member_count}")
    return embed


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _send(self, guild, member, g_cfg):
        """Resolves the configured channel and posts the embed. Returns
        (sent: bool, detail: str) so both the real join event and
        `!welcome test` report/log the exact same outcome."""
        channel_id = g_cfg.get("welcome_channel_id")
        channel, err = await cu.resolve_channel(guild, channel_id)
        if err:
            return False, err
        embed = build_embed(member, guild, g_cfg)
        try:
            await channel.send(embed=embed)
            return True, f"sent to {channel.mention}"
        except discord.Forbidden:
            return False, f"no permission to post in {channel.mention} - needs View Channel + Send Messages + Embed Links"
        except discord.HTTPException as e:
            return False, f"failed to post in {channel.mention}: {e}"

    @commands.Cog.listener()
    async def on_member_join(self, member):
        guild = member.guild
        all_cfg = cfgschema.load_cfg()
        g_cfg = cfgschema.ensure_guild(all_cfg, guild.id, guild.name)
        if not g_cfg.get("welcome_enabled"):
            return
        sent, detail = await self._send(guild, member, g_cfg)
        if not sent:
            print(f"[welcome] '{guild.name}': {detail}")

    async def _do_test(self, ctx):
        guild = ctx.guild
        if guild is None:
            return await cu.respond(ctx, "server only", ephemeral=True)
        all_cfg = cfgschema.load_cfg()
        g_cfg = cfgschema.ensure_guild(all_cfg, guild.id, guild.name)

        changed = False
        if not g_cfg.get("welcome_channel_id"):
            _, changed = cu.resolve_named_channel(guild, g_cfg, "welcome_channel_id", _NAME_ALIASES)
        if changed:
            cfgschema.save_cfg(all_cfg)

        if not g_cfg.get("welcome_enabled"):
            return await cu.respond(
                ctx, "⏸️ welcome messages are disabled for this server - turn them on in the "
                     "dashboard's **Leveling & Welcome** tab first", ephemeral=True)

        sent, detail = await self._send(guild, ctx.author, g_cfg)
        prefix = "✅" if sent else "❌"
        note = " (auto-detected a channel by name and saved it)" if changed else ""
        await cu.respond(ctx, f"{prefix} {detail}{note}", ephemeral=True)

    welcome_group = SlashCommandGroup("welcome", "welcome message settings")

    @welcome_group.command(name="test", description="send a preview welcome embed as if you just joined")
    @cu.staff_check()
    async def welcome_test_slash(self, ctx):
        await cu.maybe_defer(ctx, ephemeral=True)
        await self._do_test(ctx)

    @commands.group(name="welcome", invoke_without_command=True)
    async def welcome_prefix(self, ctx):
        await cu.respond(ctx, "usage: `!welcome test` - sends a preview welcome embed to yourself")

    @welcome_prefix.command(name="test")
    @cu.staff_check()
    async def welcome_test_prefix(self, ctx):
        await self._do_test(ctx)


def setup(bot):
    bot.add_cog(Welcome(bot))
