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
"""
import discord
from discord.ext import commands

import config_schema as cfgschema

DEFAULT_WELCOME_MESSAGE = "welcome {user} to **{server}**! you're member #{membercount} 🎉"


def render_template(template, member, guild):
    text = template
    text = text.replace("{user}", member.mention)
    text = text.replace("{username}", str(member.display_name))
    text = text.replace("{server}", guild.name)
    text = text.replace("{membercount}", str(guild.member_count))
    return text


class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member):
        guild = member.guild
        all_cfg = cfgschema.load_cfg()
        g_cfg = cfgschema.ensure_guild(all_cfg, guild.id, guild.name)

        if not g_cfg.get("welcome_enabled"):
            return
        channel_id = g_cfg.get("welcome_channel_id")
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            return

        template = cfgschema.env_first("WELCOME_MESSAGE", g_cfg.get("welcome_message"), DEFAULT_WELCOME_MESSAGE)
        text = render_template(template, member, guild)

        embed = discord.Embed(description=text, color=discord.Color.gold(), timestamp=discord.utils.utcnow())
        embed.set_author(name=f"{member} joined", icon_url=member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"member #{guild.member_count}")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            print(f"[welcome] no permission to post in the welcome channel in '{guild.name}' - "
                  f"check the bot's permissions there")
        except discord.HTTPException as e:
            print(f"[welcome] failed to post welcome message in '{guild.name}': {e}")


def setup(bot):
    bot.add_cog(Welcome(bot))
