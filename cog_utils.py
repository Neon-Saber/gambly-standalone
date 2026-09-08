"""
Tiny glue so every command in cogs/ can be written once and exposed both as
a slash command and a prefix command, mirroring how bot.py itself pairs
every economy/casino command (do_deposit() + a slash wrapper + a prefix
wrapper). A slash interaction uses ApplicationContext (ctx.respond /
ctx.defer / ctx.followup); a prefix message uses the plain Context
(ctx.send). These helpers pick the right one so the actual command logic
doesn't need to know or care which interface triggered it.
"""
import discord


async def respond(ctx, content=None, embed=None, ephemeral=False):
    if isinstance(ctx, discord.ApplicationContext):
        return await ctx.respond(content=content, embed=embed, ephemeral=ephemeral)
    if content is None and embed is None:
        return None
    return await ctx.send(content=content, embed=embed)


async def maybe_defer(ctx, ephemeral=False):
    """Slash commands can take >3s if they need to; prefix commands have no
    such limit and no defer() method at all, so this is a no-op for them."""
    if isinstance(ctx, discord.ApplicationContext):
        await ctx.defer(ephemeral=ephemeral)


async def followup(ctx, content=None, embed=None, ephemeral=False):
    if isinstance(ctx, discord.ApplicationContext):
        return await ctx.followup.send(content=content, embed=embed, ephemeral=ephemeral)
    return await ctx.send(content=content, embed=embed)
