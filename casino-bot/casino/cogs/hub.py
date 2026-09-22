"""The ``/casino`` entry point.

Two flavours of the same panel: a personal one anybody can open, and a
pinnable one for staff. The pinned version keeps working indefinitely
because :class:`casino.ui.hub.HubView` is registered as a persistent view at
startup — the thing the old dashboard claimed to do in its footer but did
not actually do.
"""

from __future__ import annotations

from typing import Any

import discord
from discord.ext import commands

from casino.core.checks import staff_only
from casino.ui import theme
from casino.ui.hub import HubView, hub_embed
from casino.ui.responder import Responder


class Hub(commands.Cog, name="Hub"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @commands.hybrid_command(name="casino", aliases=["c", "games"], description="Open the casino.")
    async def casino(self, ctx: commands.Context) -> None:
        responder = Responder(ctx)
        settings = await self.bot.guard.ensure_player(responder.guild_id, responder.user.id)
        balance = await self.bot.economy.balance(responder.guild_id, responder.user.id)
        await responder.send(
            embed=await hub_embed(self.bot, responder.user, balance, settings),
            view=HubView(self.bot),
        )

    @commands.hybrid_command(name="casinopanel", description="Post a permanent casino panel in this channel.")
    @staff_only()
    async def casinopanel(self, ctx: commands.Context) -> None:
        settings = await self.bot.guilds_store.settings(ctx.guild.id)
        embed = theme.embed(
            title="🎰 The Casino",
            description=(
                "Everything starts here. Pick a game, check your wallet, claim your daily, "
                "or look at the leaderboards.\n\n"
                "Every round is provably fair: each result publishes a hash of the seed it "
                "was generated from, and `/verify` checks it."
            ),
            color=theme.BRAND,
        )
        embed.add_field(
            name="Table limits",
            value=f"{int(settings['min_bet']):,} – {int(settings['max_bet']):,} chips",
            inline=True,
        )
        embed.add_field(
            name="Return to player",
            value=f"{float(settings['rtp']) * 100:.1f}% on house games",
            inline=True,
        )
        embed.add_field(name="Duels", value="Zero house edge, player versus player", inline=True)
        embed.set_footer(text="Virtual chips only. No real money is involved.")
        await ctx.channel.send(embed=embed, view=HubView(self.bot))
        if ctx.interaction is not None:
            await ctx.send(
                embed=theme.success("Panel posted. Pin it — its buttons survive restarts."),
                ephemeral=True,
            )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Say hello somewhere sensible, with the two commands that matter."""
        channel = guild.system_channel or next(
            (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None
        )
        if channel is None:
            return
        try:
            await channel.send(
                embed=theme.embed(
                    title="🎰 Thanks for the invite",
                    description=(
                        "**`/casino`** opens the tables.\n"
                        "**`/config`** shows every setting, including bet limits and the staff role.\n"
                        "**`/help`** lists everything else.\n\n"
                        "Chips are virtual and scoped to this server."
                    ),
                    color=theme.BRAND,
                )
            )
        except (discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot: Any) -> None:
    await bot.add_cog(Hub(bot))
