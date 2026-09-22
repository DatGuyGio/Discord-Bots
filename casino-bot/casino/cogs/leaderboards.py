"""Leaderboards you can re-slice without retyping the command.

The old ``!leaderboard`` took positional ``category`` and ``timeframe``
arguments, rejected most combinations with an error, and computed windowed
boards by loading every player's entire transaction list into Python on each
call. Here the category and window are dropdowns on the message itself, and
the windowed numbers come from one indexed SQL aggregate over the ledger.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from casino.ui import theme
from casino.ui.responder import Responder
from casino.ui.views import CasinoView

METRICS = {
    "balance": ("💰 Richest", "chips held"),
    "wagered": ("🎰 Most staked", "chips staked"),
    "net": ("📈 Best profit", "net profit"),
    "wins": ("🏆 Most wins", "rounds won"),
    "streak": ("🔥 Longest streak", "wins in a row"),
    "level": ("⭐ Highest level", "level"),
}

WINDOWS = {
    "alltime": ("All time", None),
    "daily": ("Last 24 hours", timedelta(days=1)),
    "weekly": ("Last 7 days", timedelta(days=7)),
}

#: Windowed boards read the ledger, which only records staking and winning,
#: so only these two metrics have a meaningful time-limited version.
WINDOWED_METRICS = ("wagered", "net")


class Leaderboards(commands.Cog, name="Leaderboards"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @commands.hybrid_command(name="leaderboard", aliases=["lb", "top"], description="Server leaderboards.")
    @app_commands.describe(metric="What to rank by.", window="Over what period.")
    @app_commands.choices(
        metric=[app_commands.Choice(name=label, value=key) for key, (label, _) in METRICS.items()],
        window=[app_commands.Choice(name=label, value=key) for key, (label, _) in WINDOWS.items()],
    )
    async def leaderboard(
        self, ctx: commands.Context, metric: str = "balance", window: str = "alltime"
    ) -> None:
        responder = Responder(ctx)
        embed, view = await self.build(responder.guild, responder.user, metric, window)
        await responder.send(embed=embed, view=view)

    async def show_board(self, interaction: discord.Interaction, metric: str, window: str) -> None:
        embed, view = await self.build(interaction.guild, interaction.user, metric, window)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ------------------------------------------------------------------

    async def build(
        self, guild: discord.Guild | None, user: discord.abc.User, metric: str, window: str
    ) -> tuple[discord.Embed, "BoardView"]:
        metric = metric if metric in METRICS else "balance"
        window = window if window in WINDOWS else "alltime"
        if window != "alltime" and metric not in WINDOWED_METRICS:
            window = "alltime"

        label, unit = METRICS[metric]
        window_label, span = WINDOWS[window]
        rows: list[dict[str, Any]] = []

        if guild is not None:
            if span is None:
                if metric == "level":
                    rows = [
                        {"user_id": row["user_id"], "value": row["level"], "extra": f"prestige {row['prestige']}" if row["prestige"] else ""}
                        for row in await self.bot.progression.leaderboard(guild.id, 10)
                    ]
                else:
                    rows = await self.bot.economy.leaderboard(guild.id, metric, 10)  # type: ignore[arg-type]
            else:
                since = discord.utils.utcnow() - span
                rows = await self.bot.economy.windowed_leaderboard(guild.id, metric, since, 10)  # type: ignore[arg-type]

        lines = []
        for index, row in enumerate(rows, start=1):
            member = guild.get_member(int(row["user_id"])) if guild else None
            name = member.display_name if member else f"Departed player ({row['user_id']})"
            marker = theme.MEDALS[index - 1] if index <= 3 else f"`{index:>2}.`"
            value = int(row["value"] or 0)
            rendered = theme.signed(value) if metric == "net" else f"{value:,}"
            suffix = ""
            if span is not None and metric == "wagered":
                suffix = f" · {theme.signed(row.get('delta', 0))} net"
            highlight = "**" if member and member.id == user.id else ""
            lines.append(f"{marker} {highlight}{name}{highlight} — **{rendered}** {unit}{suffix}")

        embed = theme.embed(
            title=f"{label} · {window_label}",
            description="\n".join(lines) or "Nobody has played yet.",
            color=theme.GOLD,
            footer="Use the menus to change the ranking or the period.",
        )
        return embed, BoardView(self, user, metric, window)


class BoardView(CasinoView):
    def __init__(self, cog: Leaderboards, user: discord.abc.User, metric: str, window: str) -> None:
        super().__init__(owner_id=user.id, timeout=240)
        self.cog = cog
        self.user = user
        self.metric = metric
        self.window = window

        metric_select: discord.ui.Select = discord.ui.Select(
            placeholder="Rank by…",
            options=[
                discord.SelectOption(label=label, value=key, default=key == metric)
                for key, (label, _) in METRICS.items()
            ],
            row=0,
        )
        metric_select.callback = self._metric_changed  # type: ignore[method-assign]
        self.metric_select = metric_select
        self.add_item(metric_select)

        window_select: discord.ui.Select = discord.ui.Select(
            placeholder="Period…",
            options=[
                discord.SelectOption(
                    label=label,
                    value=key,
                    default=key == window,
                    description=None if key == "alltime" else "Staking and profit only",
                )
                for key, (label, _) in WINDOWS.items()
            ],
            row=1,
        )
        window_select.callback = self._window_changed  # type: ignore[method-assign]
        self.window_select = window_select
        self.add_item(window_select)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        embed, view = await self.cog.build(interaction.guild, self.user, self.metric, self.window)
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = interaction.message

    async def _metric_changed(self, interaction: discord.Interaction) -> None:
        self.metric = self.metric_select.values[0]
        await self._refresh(interaction)

    async def _window_changed(self, interaction: discord.Interaction) -> None:
        self.window = self.window_select.values[0]
        await self._refresh(interaction)


async def setup(bot: Any) -> None:
    await bot.add_cog(Leaderboards(bot))
