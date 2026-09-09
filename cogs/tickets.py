"""
Ticket system - posts a panel with an "Open Ticket" button. Clicking it
creates a private channel (visible only to the opener + staff) under
whichever category is configured for this guild (dashboard: Moderation &
Logging -> Ticket category; .env default: TICKET_CATEGORY_ID).

Staff can claim/close from buttons inside the ticket channel. Closing posts
a full text transcript to the "ticket" log channel, if that log type is
enabled for this guild, then deletes the channel.

Buttons are persistent (custom_id-based, timeout=None) so they keep
working across bot restarts without needing the original interaction to
still be "live".
"""
import io
import time

import discord
from discord.ext import commands
from discord import Option

import logging_utils
import embeds
import cog_utils as cu


def ticket_channel_name(member: discord.Member) -> str:
    base = "".join(c for c in member.name.lower() if c.isalnum()) or "user"
    return f"ticket-{base}"[:90]


class TicketPanelView(discord.ui.View):
    """Posted once in whichever channel staff choose. Persistent (timeout=None)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.green,
                       emoji="🎫", custom_id="ticket_panel:open")
    async def open_ticket(self, button: discord.ui.Button, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user

        existing = discord.utils.get(guild.text_channels, name=ticket_channel_name(member))
        if existing:
            return await interaction.response.send_message(
                f"you already have an open ticket: {existing.mention}", ephemeral=True)

        settings = logging_utils.get_ticket_settings(guild.id)
        category = guild.get_channel(int(settings["category_id"])) if settings["category_id"] else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                 read_message_history=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                   manage_channels=True, read_message_history=True),
        }
        staff_role_id = logging_utils.get_staff_role_id(guild.id)
        if staff_role_id:
            staff_role = guild.get_role(int(staff_role_id))
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True)

        try:
            channel = await guild.create_text_channel(
                name=ticket_channel_name(member),
                category=category,
                overwrites=overwrites,
                topic=f"ticket opened by {member} ({member.id}) at {int(time.time())}",
                reason=f"ticket opened by {member}",
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "I don't have permission to create channels here - ask an admin to check my role's "
                "permissions (Manage Channels) and try again.", ephemeral=True)

        ping = f"<@&{settings['ping_role_id']}> " if settings["ping_role_id"] else ""
        embed = discord.Embed(
            title="ticket opened",
            description=f"thanks for reaching out, {member.mention} — staff will be with you shortly.\n"
                        f"describe your issue below. use the buttons here to **claim** or **close** this ticket.",
            color=embeds.COLOR_INFO,
        )
        await channel.send(content=f"{ping}{member.mention}", embed=embed, view=TicketControlView())
        await interaction.response.send_message(f"🎫 ticket created: {channel.mention}", ephemeral=True)


class TicketControlView(discord.ui.View):
    """Posted inside each ticket channel. Persistent (timeout=None)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.blurple,
                       emoji="🙋", custom_id="ticket_panel:claim")
    async def claim(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not logging_utils.is_staff(interaction.user):
            return await interaction.response.send_message("staff only", ephemeral=True)
        await interaction.response.send_message(f"🙋 **{interaction.user.mention}** claimed this ticket")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.red,
                       emoji="🔒", custom_id="ticket_panel:close")
    async def close(self, button: discord.ui.Button, interaction: discord.Interaction):
        opener_id = _opener_id_from_topic(interaction.channel)
        if not logging_utils.is_staff(interaction.user) and interaction.user.id != opener_id:
            return await interaction.response.send_message("only staff or the ticket opener can close this", ephemeral=True)
        await interaction.response.send_message("🔒 closing this ticket in a few seconds...")
        await _close_ticket(interaction.channel, interaction.user)


def _opener_id_from_topic(channel: discord.TextChannel):
    if not channel.topic or "(" not in channel.topic:
        return None
    try:
        return int(channel.topic.split("(")[1].split(")")[0])
    except (ValueError, IndexError):
        return None


async def _close_ticket(channel: discord.TextChannel, closer: discord.Member):
    opener_id = _opener_id_from_topic(channel)

    # build a plain-text transcript before the channel disappears
    lines = [f"transcript for #{channel.name} — closed by {closer} ({closer.id})", ""]
    try:
        async for msg in channel.history(limit=500, oldest_first=True):
            ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            content = msg.content or "[embed/attachment]"
            lines.append(f"[{ts}] {msg.author}: {content}")
    except discord.HTTPException:
        pass

    embed = embeds.ticket_closed_embed(channel, closer, opener_id)
    transcript = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{channel.name}.txt")
    log_channel = logging_utils.resolve_log_channel(channel.guild, "ticket")
    if log_channel:
        try:
            await log_channel.send(embed=embed, file=transcript)
        except discord.HTTPException:
            pass

    try:
        await channel.delete(reason=f"ticket closed by {closer}")
    except discord.Forbidden:
        pass


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._views_registered = False

    @commands.Cog.listener()
    async def on_ready(self):
        # re-register persistent views once the gateway connection (and its
        # event loop) is actually up, so buttons posted before a restart
        # keep working. views can't be constructed before there's a running
        # loop, so this can't happen in __init__.
        if self._views_registered:
            return
        self._views_registered = True
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(TicketControlView())

    async def _do_ticket_panel(self, ctx, title, description):
        embed = discord.Embed(title=title, description=description, color=embeds.COLOR_INFO)
        await ctx.channel.send(embed=embed, view=TicketPanelView())
        await cu.respond(ctx, "panel posted", ephemeral=True)

    @commands.slash_command(name="ticket-panel", description="post the ticket panel in this channel (staff only)")
    @commands.has_permissions(manage_guild=True)
    async def ticket_panel(self, ctx,
                           title: Option(str, "panel title", required=False) = "Need help?",
                           description: Option(str, "panel body text", required=False) =
                           "Click below to open a private ticket with staff."):
        await self._do_ticket_panel(ctx, title, description)

    @commands.command(name="ticket-panel")
    @commands.has_permissions(manage_guild=True)
    async def ticket_panel_cmd(self, ctx, title: str = "Need help?", *,
                               description: str = "Click below to open a private ticket with staff."):
        await self._do_ticket_panel(ctx, title, description)

    async def _do_close_ticket(self, ctx):
        if not ctx.channel.name.startswith("ticket-"):
            return await cu.respond(ctx, "this isn't a ticket channel", ephemeral=True)
        opener_id = _opener_id_from_topic(ctx.channel)
        if not logging_utils.is_staff(ctx.author) and ctx.author.id != opener_id:
            return await cu.respond(ctx, "only staff or the ticket opener can close this", ephemeral=True)
        await cu.respond(ctx, "🔒 closing this ticket in a few seconds...")
        await _close_ticket(ctx.channel, ctx.author)

    @commands.slash_command(name="close-ticket", description="close the current ticket (run inside a ticket channel)")
    async def close_ticket_cmd(self, ctx):
        await self._do_close_ticket(ctx)

    @commands.command(name="close-ticket")
    async def close_ticket_prefix_cmd(self, ctx):
        await self._do_close_ticket(ctx)

    async def _do_add_to_ticket(self, ctx, member):
        if not ctx.channel.name.startswith("ticket-"):
            return await cu.respond(ctx, "this isn't a ticket channel", ephemeral=True)
        await ctx.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
        await cu.respond(ctx, f"added {member.mention} to this ticket")

    @commands.slash_command(name="add-to-ticket", description="add someone to the current ticket (staff only)")
    @commands.has_permissions(manage_guild=True)
    async def add_to_ticket(self, ctx, member: Option(discord.Member, "who")):
        await self._do_add_to_ticket(ctx, member)

    @commands.command(name="add-to-ticket")
    @commands.has_permissions(manage_guild=True)
    async def add_to_ticket_cmd(self, ctx, member: discord.Member):
        await self._do_add_to_ticket(ctx, member)


def setup(bot):
    bot.add_cog(Tickets(bot))
