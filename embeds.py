"""
Shared embed builders. Keeping these in one place means every log embed
(moderation, message edits/deletes, reports, withdrawals/deposits, tickets)
looks consistent - same color scheme, same field layout, same timestamp
format - instead of each cog inventing its own.
"""
import discord

COLOR_INFO = discord.Color.blurple()
COLOR_GOOD = discord.Color.green()
COLOR_WARN = discord.Color.orange()
COLOR_SEVERE = discord.Color.red()


def _base_embed(title, color):
    return discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())


def user_field(embed, label, user):
    embed.add_field(name=label, value=f"{user.mention} (`{user.id}`)", inline=True)


def bot_status_embed(guild, opener, ping_ms, ping_note, uptime_str, version_str):
    """Friendly "how's it going" status embed - see cogs/bot_status.py for
    the human-toned copy this wraps. One spoken-style opener line plus an
    online indicator, then latency/uptime/version stacked as their own
    rows (not side-by-side) so the card has some vertical presence instead
    of reading like a single cramped line - no servers/members/bot-tag,
    still just a quick glance rather than a stats dump."""
    embed = _base_embed(f"{guild.name} status", COLOR_INFO)
    embed.description = f"{opener}\n\n✅ **Online**"
    embed.add_field(name="🏓 latency", value=f"{ping_ms}ms - {ping_note}", inline=False)
    embed.add_field(name="⏱️ uptime", value=uptime_str, inline=False)
    embed.add_field(name="🏷️ version", value=version_str, inline=False)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="checks in periodically - run /status anytime for a fresh read")
    return embed


def bot_status_stopped_embed(guild, reason):
    """Posted in place of the usual status embed right as the bot shuts
    down (see bot.py's shutdown handler + BotStatus.mark_stopped) so the
    channel shows an accurate "stopped, here's why" instead of leaving the
    last "Online" post up looking fine when the bot is actually down.
    Same tall single-column shape as bot_status_embed for a consistent
    look between the two states."""
    embed = _base_embed(f"{guild.name} status", COLOR_SEVERE)
    embed.description = "🔴 **Stopped**"
    embed.add_field(name="reason", value=reason or "no reason given", inline=False)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="will post a fresh status once it's back up")
    return embed


def mod_action_embed(action_title, member, moderator, reason=None, color=COLOR_INFO, extra=None):
    """member/moderator can be any discord.abc.User (Member or plain User -
    ban-by-ID hands us a User, not a Member, when they're not in the server)."""
    embed = _base_embed(action_title, color)
    user_field(embed, "user", member)
    user_field(embed, "moderator", moderator)
    embed.add_field(name="reason", value=reason or "no reason given", inline=False)
    if extra:
        for k, v in extra.items():
            embed.add_field(name=k, value=str(v), inline=True)
    if hasattr(member, "display_avatar"):
        embed.set_thumbnail(url=member.display_avatar.url)
    return embed


def message_edit_embed(message, before_content, after_content):
    embed = _base_embed("message edited", COLOR_WARN)
    user_field(embed, "user", message.author)
    embed.add_field(name="channel", value=f"{message.channel.mention} (`{message.channel.id}`)", inline=True)
    embed.add_field(name="jump", value=f"[go to message]({message.jump_url})", inline=True)
    embed.add_field(name="before", value=(before_content or "*empty*")[:1024], inline=False)
    embed.add_field(name="after", value=(after_content or "*empty*")[:1024], inline=False)
    if hasattr(message.author, "display_avatar"):
        embed.set_thumbnail(url=message.author.display_avatar.url)
    return embed


def message_delete_embed(message):
    embed = _base_embed("message deleted", COLOR_SEVERE)
    user_field(embed, "user", message.author)
    embed.add_field(name="channel", value=f"{message.channel.mention} (`{message.channel.id}`)", inline=True)
    embed.add_field(name="content", value=(message.content or "*empty / embed or attachment only*")[:1024], inline=False)
    if message.attachments:
        embed.add_field(name="attachments", value="\n".join(a.url for a in message.attachments[:5]), inline=False)
    if hasattr(message.author, "display_avatar"):
        embed.set_thumbnail(url=message.author.display_avatar.url)
    return embed


def report_embed(reporter, reported_user, reason, channel=None, message_link=None, message_content=None):
    embed = _base_embed("new report", COLOR_WARN)
    user_field(embed, "reported by", reporter)
    if reported_user:
        user_field(embed, "reported user", reported_user)
    if channel:
        embed.add_field(name="channel", value=f"{channel.mention} (`{channel.id}`)", inline=True)
    embed.add_field(name="reason", value=reason, inline=False)
    if message_content:
        embed.add_field(name="message content", value=message_content[:1024], inline=False)
    if message_link:
        embed.add_field(name="jump", value=f"[go to message]({message_link})", inline=False)
    if reported_user and hasattr(reported_user, "display_avatar"):
        embed.set_thumbnail(url=reported_user.display_avatar.url)
    return embed


def transaction_embed(kind, member, amount, wallet_after, bank_after):
    """kind is 'deposit' or 'withdraw'."""
    title = "deposit" if kind == "deposit" else "withdrawal"
    embed = _base_embed(f"{title} — {member}", COLOR_GOOD if kind == "deposit" else COLOR_INFO)
    user_field(embed, "user", member)
    embed.add_field(name="amount", value=f"{amount:,} chips", inline=True)
    embed.add_field(name="wallet after", value=f"{wallet_after:,} chips", inline=True)
    embed.add_field(name="bank after", value=f"{bank_after:,} chips", inline=True)
    embed.add_field(name="status", value="✅ completed", inline=True)
    if hasattr(member, "display_avatar"):
        embed.set_thumbnail(url=member.display_avatar.url)
    return embed


def ticket_closed_embed(channel, closer, opener_id=None):
    embed = _base_embed("ticket closed", COLOR_SEVERE)
    embed.add_field(name="channel", value=f"#{channel.name} (`{channel.id}`)", inline=True)
    embed.add_field(name="closed by", value=closer.mention, inline=True)
    if opener_id:
        embed.add_field(name="opened by", value=f"<@{opener_id}> (`{opener_id}`)", inline=True)
    return embed
