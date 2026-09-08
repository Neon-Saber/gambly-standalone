"""
Message logging - edits and deletes, sent to the guild's "message" log
channel (dashboard: Moderation & Logging -> Message logs; .env default:
MESSAGE_LOG_CHANNEL_ID) if that log type is enabled.

Bot messages (including this bot's own) are skipped so log channels don't
fill up with the bot's own game-result messages disappearing/getting
edited, and edits where the text content didn't actually change (an embed
loading in, a link unfurling) are skipped too - neither is useful to staff
and both would otherwise spam the log channel on ordinary use.
"""
from discord.ext import commands

import logging_utils
import embeds


class LoggingEvents(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if message.author.bot or message.guild is None:
            return
        await logging_utils.send_log(self.bot, message.guild, "message", embeds.message_delete_embed(message))

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if before.author.bot or before.guild is None:
            return
        if before.content == after.content:
            return
        await logging_utils.send_log(self.bot, after.guild, "message",
                                      embeds.message_edit_embed(after, before.content, after.content))


def setup(bot):
    bot.add_cog(LoggingEvents(bot))
