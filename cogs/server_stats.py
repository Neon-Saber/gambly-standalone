"""
Live "Members: N" voice-channel counter (bots excluded from the count).
Fully dashboard-driven (Leveling & Welcome tab) - enabled/disabled, which
channel, and the name template. Channel follows this project's usual
one-time-env-default-then-dashboard pattern; the template uses
config_schema.env_first() like the welcome/level-up messages do.

Discord silently rate-limits channel NAME edits specifically to roughly
2 per 10 minutes per channel (separate from, and much tighter than, its
general API rate limits) - hammering this on every join/leave in an
active server would start silently failing/lagging behind. So updates
are cooldown-gated: a join/leave tries an immediate update but backs off
if one already happened recently, and a 10-minute background loop
always catches it up regardless. Never edits if the name wouldn't
actually change, to avoid burning quota for nothing.
"""
import time

import discord
from discord.ext import commands, tasks

import config_schema as cfgschema

DEFAULT_MEMBER_COUNT_TEMPLATE = "Members: {count}"
UPDATE_COOLDOWN = 600  # seconds - stays under Discord's channel-rename rate limit


class ServerStats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._last_edit = {}  # guild id -> unix ts of the last actual channel rename
        self.sync_loop.start()

    def cog_unload(self):
        self.sync_loop.cancel()

    async def _sync_guild(self, guild, force=False):
        if guild is None:
            return
        all_cfg = cfgschema.load_cfg()
        g_cfg = cfgschema.ensure_guild(all_cfg, guild.id, guild.name)
        if not g_cfg.get("member_count_enabled"):
            return
        channel_id = g_cfg.get("member_count_channel_id")
        if not channel_id:
            print(f"[server_stats] enabled in '{guild.name}' but no voice channel is set - "
                  f"pick one in the dashboard's Leveling & Welcome tab")
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            # not in the bot's cache for some reason - do a real API call
            # before giving up, instead of silently doing nothing.
            try:
                channel = await guild.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden) as e:
                print(f"[server_stats] configured member-count channel ({channel_id}) in '{guild.name}' "
                      f"doesn't exist or the bot can't see it: {e}")
                return
            except discord.HTTPException as e:
                print(f"[server_stats] couldn't resolve member-count channel ({channel_id}) in '{guild.name}': {e}")
                return

        if not force and time.time() - self._last_edit.get(guild.id, 0) < UPDATE_COOLDOWN:
            return  # too soon - the periodic loop below will catch this up

        count = sum(1 for m in guild.members if not m.bot)
        template = cfgschema.env_first("MEMBER_COUNT_TEMPLATE", g_cfg.get("member_count_template"),
                                        DEFAULT_MEMBER_COUNT_TEMPLATE)
        new_name = template.replace("{count}", str(count))
        if channel.name == new_name:
            return

        try:
            await channel.edit(name=new_name)
            self._last_edit[guild.id] = time.time()
        except discord.Forbidden:
            print(f"[server_stats] no permission to rename the member-count channel in '{guild.name}' - "
                  f"check the bot has Manage Channel there")
        except discord.HTTPException as e:
            print(f"[server_stats] failed to rename the member-count channel in '{guild.name}': {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self._sync_guild(guild, force=True)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await self._sync_guild(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await self._sync_guild(member.guild)

    @tasks.loop(minutes=10)
    async def sync_loop(self):
        for guild in self.bot.guilds:
            await self._sync_guild(guild, force=True)

    @sync_loop.before_loop
    async def before_sync_loop(self):
        await self.bot.wait_until_ready()


def setup(bot):
    bot.add_cog(ServerStats(bot))
