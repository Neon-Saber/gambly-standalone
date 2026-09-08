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
