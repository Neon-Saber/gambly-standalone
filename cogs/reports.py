"""
User reporting. Two ways in:
- `/report <user> <reason> [evidence]` - report a member directly.
- right-click a message -> Apps -> "Report Message" - reports whoever sent
  that specific message, with the message's own content/link attached
  automatically so staff don't have to go find it.

Either way, the report goes to the guild's "report" log channel (dashboard:
Moderation & Logging -> Reports; .env default: REPORT_LOG_CHANNEL_ID) if
that log type is enabled, and the reporter gets an ephemeral confirmation
either way (staff not being notified isn't the reporter's fault, so we
still tell them "submitted" - just honestly, if it didn't actually reach a
channel).
"""
import discord
from discord.ext import commands
from discord import Option

import logging_utils
import embeds
import cog_utils as cu


class Reports(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _submit(self, ctx_or_interaction, guild, reporter, reported_user, reason,
                      channel=None, message_link=None, message_content=None, evidence=None):
        embed = embeds.report_embed(reporter, reported_user, reason, channel, message_link, message_content)
        if evidence is not None:
            embed.add_field(name="evidence", value=f"[attachment]({evidence.url})", inline=False)
            if evidence.content_type and evidence.content_type.startswith("image/"):
                embed.set_image(url=evidence.url)
        sent = await logging_utils.send_log(self.bot, guild, "report", embed)
        return sent

    async def _do_report(self, ctx, member, reason, evidence=None):
        if member.id == ctx.author.id:
            return await cu.respond(ctx, "you can't report yourself", ephemeral=True)
        sent = await self._submit(ctx, ctx.guild, ctx.author, member, reason, channel=ctx.channel, evidence=evidence)
        if sent:
            await cu.respond(ctx, "✅ report submitted — a staff member will take a look.", ephemeral=True)
        else:
            await cu.respond(ctx, "your report was recorded, but reporting isn't fully set up on this server yet "
                             "(no report channel configured) - please also flag this to staff directly if it's urgent.",
                             ephemeral=True)

    @commands.slash_command(name="report", description="report a member to the staff team")
    async def report(self, ctx,
                     member: Option(discord.Member, "who you're reporting"),
                     reason: Option(str, "what happened"),
                     evidence: Option(discord.Attachment, "screenshot or other evidence", required=False) = None):
        await self._do_report(ctx, member, reason, evidence)

    @commands.command(name="report")
    async def report_cmd(self, ctx, member: discord.Member, *, reason: str):
        evidence = ctx.message.attachments[0] if ctx.message.attachments else None
        await self._do_report(ctx, member, reason, evidence)

    @commands.message_command(name="Report Message")
    async def report_message(self, ctx, message: discord.Message):
        if message.author.id == ctx.author.id:
            return await ctx.respond("you can't report your own message", ephemeral=True)
        if message.author.bot:
            return await ctx.respond("can't report a bot's message", ephemeral=True)
        sent = await self._submit(
            ctx, ctx.guild, ctx.author, message.author, "reported via message context menu",
            channel=message.channel, message_link=message.jump_url, message_content=message.content,
        )
        if sent:
            await ctx.respond("✅ message reported — a staff member will take a look.", ephemeral=True)
        else:
            await ctx.respond("your report was recorded, but reporting isn't fully set up on this server yet "
                              "(no report channel configured) - please also flag this to staff directly if it's urgent.",
                              ephemeral=True)
        # note: message context-menu commands are a right-click-only Discord
        # UI feature - there's no meaningful "prefix" equivalent for them,
        # same as in the original bot's era. use /report or !report instead
        # if you want to report from a typed command.


def setup(bot):
    bot.add_cog(Reports(bot))
