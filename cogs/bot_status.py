"""
Bot status embed - a periodic "how's it going" check-in posted to a channel,
plus an on-demand /status and !status command that works anywhere without
needing that channel configured at all.

Fully dashboard-driven (Moderation & Logging tab), same one-time-env-default-
then-dashboard pattern as welcome/leveling/member-count: set STATUS_CHANNEL_ID
in .env to seed a server's channel the first time its config is created,
after that the dashboard owns it. Set/change it anytime from the dashboard
without a restart.

THE "make it look human" PART: one spoken-style opener line (a handful to
pick from, so it's not the exact same line every cycle), a plain "Online"
indicator, then a compact three-field grid: latency, uptime, version. No
servers, no member count, no bot tag, no economy figures - those made it
read like a stats dump instead of a quick glance.

Uptime is tracked from when this cog loads (i.e. since the last bot
restart) - there's no persistence across restarts on purpose, since
"uptime" should mean exactly that.

Version comes from version.py (git commit count, e.g. "v128") - nothing to
remember to bump by hand; it just moves on its own every real deploy.

The periodic post EDITS one message in place (like server_stats' member-
count channel) rather than sending a new one every cycle, so a channel with
this enabled doesn't slowly fill up with old status posts. If that message
ever gets deleted or the channel gets swapped, this just posts a fresh one
and starts tracking that instead - see status_message_id in config_schema.py.

WHY _sync_guild IS WRAPPED IN store.locked(): on_ready fires for every
guild AND status_loop runs its first pass the moment the bot's ready
(tasks.loop starts immediately once before_loop's wait_until_ready()
returns) - so both can call _sync_guild for the same guild back to back.
Without a lock, both read config.json before either has saved, both see
no status_message_id yet, and both send a brand new message - the classic
double-post. The lock forces the second call to wait for the first's
save to land, then it re-reads config (now has the message id) and edits
instead. Same load-mutate-save race store.py's docstring already covers
for the dashboard-vs-bot case; this is bot-vs-bot on top of that.

SHUTDOWN: bot.py's signal handler calls mark_stopped(reason) right before
closing the connection, which edits every enabled guild's status message
to a red "Stopped" state with that reason instead of leaving the last
"Online" post looking fine while the bot is actually down. See bot.py for
how the reason itself gets supplied (a stop_reason.txt file, or typed at
the console if it's an interactive session).
"""
import random

import discord
from discord.ext import commands, tasks

import config_schema as cfgschema
import cog_utils as cu
import embeds
import store
import version

UPDATE_INTERVAL_MINUTES = 15

OPENERS = [
    "still here, still dealing cards.",
    "quick check-in from the house.",
    "everything's ticking along fine.",
    "just doing my rounds.",
    "nothing to report - business as usual.",
    "checking in, all's well on my end.",
]

PING_GOOD = ["feeling snappy", "quick as ever", "running smooth"]
PING_OK = ["a little sluggish, but nothing worth worrying about", "not the fastest right now, still working fine"]
PING_BAD = ["dragging a bit - might be discord's side, might be worth a peek at the VM if it keeps up"]


class BotStatus(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.start_time = discord.utils.utcnow()
        self.status_loop.start()

    def cog_unload(self):
        self.status_loop.cancel()

    def _uptime_str(self):
        delta = discord.utils.utcnow() - self.start_time
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes = remainder // 60
        parts = []
        if days:
            parts.append(f"{days}d")
        parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    async def _build_embed(self, guild):
        ping_ms = round(self.bot.latency * 1000)
        if ping_ms < 150:
            ping_note = random.choice(PING_GOOD)
        elif ping_ms < 350:
            ping_note = random.choice(PING_OK)
        else:
            ping_note = random.choice(PING_BAD)

        return embeds.bot_status_embed(
            guild, random.choice(OPENERS), ping_ms, ping_note, self._uptime_str(), version.VERSION
        )

    async def _sync_guild(self, guild):
        async with store.locked(cfgschema.CFG_FILE):
            all_cfg = cfgschema.load_cfg()
            g_cfg = cfgschema.ensure_guild(all_cfg, guild.id, guild.name)
            if not g_cfg.get("status_enabled"):
                return
            channel_id = g_cfg.get("status_channel_id")
            if not channel_id:
                print(f"[bot_status] enabled in '{guild.name}' but no channel is set - "
                      f"pick one in the dashboard's Moderation & Logging tab")
                return
            channel, err = await cu.resolve_channel(guild, channel_id)
            if err:
                print(f"[bot_status] '{guild.name}': {err}")
                return

            embed = await self._build_embed(guild)

            msg = None
            msg_id = g_cfg.get("status_message_id")
            if msg_id:
                try:
                    msg = await channel.fetch_message(int(msg_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                    msg = None  # deleted, or we can't see it anymore - fall through and post fresh

            try:
                if msg:
                    await msg.edit(embed=embed)
                else:
                    new_msg = await channel.send(embed=embed)
                    g_cfg["status_message_id"] = str(new_msg.id)
                    cfgschema.save_cfg(all_cfg)
            except discord.Forbidden:
                print(f"[bot_status] no permission to post/edit in the status channel in '{guild.name}' - "
                      f"check the bot has Send Messages/Embed Links there")
            except discord.HTTPException as e:
                print(f"[bot_status] failed to update the status embed in '{guild.name}': {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self._sync_guild(guild)

    @tasks.loop(minutes=UPDATE_INTERVAL_MINUTES)
    async def status_loop(self):
        for guild in self.bot.guilds:
            await self._sync_guild(guild)

    @status_loop.before_loop
    async def before_status_loop(self):
        await self.bot.wait_until_ready()

    async def mark_stopped(self, reason):
        """Called once from bot.py's shutdown handler, right before the
        connection closes. Best-effort: if a guild has no status message
        yet, or the edit fails for any reason, we're shutting down anyway
        so there's nothing useful to do but move on to the next guild."""
        self.status_loop.cancel()
        all_cfg = cfgschema.load_cfg()
        for guild in self.bot.guilds:
            g_cfg = cfgschema.ensure_guild(all_cfg, guild.id, guild.name)
            if not g_cfg.get("status_enabled"):
                continue
            channel_id = g_cfg.get("status_channel_id")
            msg_id = g_cfg.get("status_message_id")
            if not channel_id or not msg_id:
                continue
            channel, err = await cu.resolve_channel(guild, channel_id)
            if err:
                continue
            try:
                msg = await channel.fetch_message(int(msg_id))
                await msg.edit(embed=embeds.bot_status_stopped_embed(guild, reason))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass

    async def _do_status(self, ctx):
        if not ctx.guild:
            await cu.respond(ctx, "this only makes sense inside a server", ephemeral=True)
            return
        embed = await self._build_embed(ctx.guild)
        await cu.respond(ctx, embed=embed)

    @commands.slash_command(name="status", description="check in on the bot")
    async def status(self, ctx):
        await self._do_status(ctx)

    @commands.command(name="status")
    async def status_cmd(self, ctx):
        await self._do_status(ctx)


def setup(bot):
    bot.add_cog(BotStatus(bot))
