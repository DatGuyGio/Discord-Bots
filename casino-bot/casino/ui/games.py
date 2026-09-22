"""Game-specific interaction surfaces: the mines grid, the crash ride, the
roulette bet board, and the replay controls attached to every result.

The design rule here is *one message per round*. A round of mines edits a
single message as you click; a crash round edits a single message as the
multiplier climbs. The old bot posted a new message for each step of a
blackjack turn, so a single hand could leave eight messages in the channel.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import discord

from casino.core import rounds
from casino.core.errors import RoundFinished
from casino.core.rounds import conclude
from casino.games import crash as crash_game
from casino.games import mines as mines_game
from casino.games import roulette as roulette_game
from casino.games.rng import Fairness
from casino.ui import theme
from casino.ui.views import AmountModal, CasinoView

PlayCallback = Callable[[discord.Interaction, int], Awaitable[Any]]


# --------------------------------------------------------------------------
# Shared result presentation
# --------------------------------------------------------------------------


def result_embed(
    *,
    game: str,
    icon: str,
    headline: str,
    body: str,
    result: rounds.RoundResult,
    fairness: Fairness | None = None,
    user: discord.abc.User | None = None,
) -> discord.Embed:
    """The house style for "here is how your round went"."""
    settlement = result.settlement
    embed = theme.embed(
        title=f"{icon} {game} · {headline}",
        description=f"{body}\n\n{theme.result_block(settlement.stake, settlement.net, settlement.balance)}",
        color=theme.outcome_color(settlement.net),
        author=user,
        footer=rounds.fairness_footer(fairness) if fairness else None,
    )
    return rounds.decorate(embed, result)


class ReplayView(CasinoView):
    """Play the same bet again without retyping it.

    A single click to re-bet is the difference between a bot you use once and
    a bot you keep using. The stake is re-validated on every replay, so a
    replay can never overdraw or exceed the server's maximum.
    """

    def __init__(
        self,
        *,
        owner_id: int,
        stake: int,
        play: PlayCallback,
        allow_double: bool = True,
        label: str | None = None,
    ) -> None:
        super().__init__(owner_id=owner_id, timeout=300)
        self.stake = stake
        self.play = play
        self.again.label = label or f"Bet {theme.compact(stake)} again"
        self.double.label = f"Double to {theme.compact(stake * 2)}"
        if not allow_double:
            self.remove_item(self.double)

    @discord.ui.button(label="Again", emoji="🔁", style=discord.ButtonStyle.success)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.play(interaction, self.stake)

    @discord.ui.button(label="Double", emoji="⏫", style=discord.ButtonStyle.primary)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.play(interaction, self.stake * 2)

    @discord.ui.button(label="Casino", emoji="🎰", style=discord.ButtonStyle.secondary)
    async def hub(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from casino.ui.hub import HubView, hub_embed

        bot = interaction.client
        balance = await bot.economy.balance(interaction.guild_id, interaction.user.id)
        settings = await bot.guilds_store.settings(interaction.guild_id)
        await interaction.response.send_message(
            embed=await hub_embed(bot, interaction.user, balance, settings),
            view=HubView(bot),
            ephemeral=True,
        )


# --------------------------------------------------------------------------
# Mines
# --------------------------------------------------------------------------

HIDDEN_TILE = "\u2003"  # an em space: an empty-looking but valid button label


def mines_embed(
    board: mines_game.Board,
    stake: int,
    *,
    user: discord.abc.User,
    fairness: Fairness,
    finished_note: str | None = None,
) -> discord.Embed:
    potential = int(stake * board.current_multiplier)
    next_value = int(stake * board.next_multiplier)
    lines = [
        f"**Mines** {board.mines} of {mines_game.TILES} · **Safe picks** {board.picks}/{board.safe_tiles}",
        "",
        f"**Banked if you stop now** {theme.chips(potential)} ({theme.multiplier(board.current_multiplier)})",
    ]
    if not board.finished:
        lines.append(
            f"**Next safe tile** {theme.chips(next_value)} ({theme.multiplier(board.next_multiplier)})"
        )
        lines.append(f"**Chance it is safe** {theme.percent((board.safe_tiles - board.picks) / (mines_game.TILES - board.picks))}")
    if finished_note:
        lines += ["", finished_note]
    return theme.embed(
        title=f"{theme.ICON['mines']} Mines",
        description="\n".join(lines),
        color=theme.TABLE if not board.finished else theme.BRAND,
        author=user,
        footer=rounds.fairness_footer(fairness),
    )


class MineTile(discord.ui.Button):
    def __init__(self, index: int, row: int) -> None:
        super().__init__(label=HIDDEN_TILE, style=discord.ButtonStyle.secondary, row=row)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "MinesView" = self.view  # type: ignore[assignment]
        await view.click(interaction, self.index)


class MinesView(CasinoView):
    """The grid. Every click either advances the multiplier or ends the round."""

    def __init__(
        self,
        bot: Any,
        user: discord.abc.User,
        wager: Any,
        board: mines_game.Board,
        fairness: Fairness,
        play: PlayCallback,
    ) -> None:
        super().__init__(owner_id=user.id, timeout=600)
        self.bot = bot
        self.user = user
        self.wager = wager
        self.board = board
        self.fairness = fairness
        self.play = play
        self._lock = asyncio.Lock()
        # The two control buttons come from the decorators below and already
        # sit on row 4; the tiles fill rows 0-3.
        self.tiles: list[MineTile] = []
        for index in range(mines_game.TILES):
            tile = MineTile(index, row=index // mines_game.WIDTH)
            self.tiles.append(tile)
            self.add_item(tile)

    # -- controls ----------------------------------------------------------

    @discord.ui.button(label="Cash out", emoji="💰", style=discord.ButtonStyle.success, row=4)
    async def cash_out_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.board.finished:
                raise RoundFinished()
            if self.board.picks == 0:
                # Nothing revealed yet: hand the stake straight back rather
                # than booking a zero-multiplier "win".
                await self.bot.economy.refund(self.wager, "mines cancelled")
                self.disable_all()
                return await interaction.response.edit_message(
                    embed=theme.embed(
                        title=f"{theme.ICON['mines']} Mines cancelled",
                        description=f"Your {theme.chips(self.wager.stake)} stake was returned.",
                        color=theme.PUSH,
                        author=self.user,
                    ),
                    view=None,
                )
            self.board.cash_out()
            await self._finish(interaction, note=f"{theme.ICON['ok']} Banked at {theme.multiplier(self.board.current_multiplier)}.")

    @discord.ui.button(label="Random tile", emoji="🎯", style=discord.ButtonStyle.secondary, row=4)
    async def random_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        remaining = [tile.index for tile in self.tiles if not tile.disabled]
        if not remaining:
            raise RoundFinished()
        from casino.games.rng import system_random

        await self.click(interaction, system_random.choice(remaining))

    # -- gameplay ----------------------------------------------------------

    async def click(self, interaction: discord.Interaction, index: int) -> None:
        async with self._lock:
            if self.board.finished:
                raise RoundFinished()
            safe = self.board.reveal(index)
            tile = self.tiles[index]
            tile.disabled = True
            if not safe:
                tile.label = "💥"
                tile.style = discord.ButtonStyle.danger
                return await self._finish(
                    interaction,
                    note=f"{theme.ICON['lose']} You hit a mine on tile {index + 1}.",
                    reveal_mines=True,
                )
            tile.label = "💎"
            tile.style = discord.ButtonStyle.success
            if self.board.cleared:
                return await self._finish(
                    interaction,
                    note=f"{theme.ICON['gem']} Board cleared — every safe tile found.",
                    reveal_mines=True,
                )
            await interaction.response.edit_message(
                embed=mines_embed(self.board, self.wager.stake, user=self.user, fairness=self.fairness),
                view=self,
            )

    async def _finish(
        self, interaction: discord.Interaction, *, note: str, reveal_mines: bool = False
    ) -> None:
        payout = self.board.payout(self.wager.stake)
        result = await conclude(
            self.bot, self.user, self.wager, payout,
            note=f"mines {self.board.mines}/{self.board.picks}",
            guild=interaction.guild,
        )
        if reveal_mines:
            for index in self.board.mine_positions:
                tile = self.tiles[index]
                if tile.label != "💥":
                    tile.label = "💣"
                    tile.style = discord.ButtonStyle.secondary
        self.disable_all()
        embed = result_embed(
            game="Mines",
            icon=theme.ICON["mines"],
            headline=f"{self.board.picks} safe · {theme.multiplier(self.board.current_multiplier)}"
            if payout
            else "Boom",
            body=note + "\n\n" + self._grid_summary(),
            result=result,
            fairness=self.fairness,
            user=self.user,
        )
        replay = ReplayView(owner_id=self.user.id, stake=self.wager.stake, play=self.play)
        await interaction.response.edit_message(embed=embed, view=replay)
        replay.message = interaction.message
        self.stop()

    def _grid_summary(self) -> str:
        rows = []
        for row in range(mines_game.HEIGHT):
            cells = []
            for column in range(mines_game.WIDTH):
                index = row * mines_game.WIDTH + column
                if index == self.board.exploded_at:
                    cells.append("💥")
                elif index in self.board.mine_positions:
                    cells.append("💣")
                elif index in self.board.revealed:
                    cells.append("💎")
                else:
                    cells.append("⬛")
            rows.append("".join(cells))
        return "\n".join(rows)


# --------------------------------------------------------------------------
# Crash
# --------------------------------------------------------------------------


def crash_embed(
    game_round: crash_game.Round,
    *,
    user: discord.abc.User,
    fairness: Fairness,
    status: str | None = None,
) -> discord.Embed:
    value = int(game_round.stake * game_round.current)
    ticks = min(len(game_round.history), 20)
    trail = "".join("▁▂▃▄▅▆▇█"[min(7, index * 8 // max(1, ticks))] for index in range(ticks))
    lines = [
        f"# {theme.multiplier(game_round.current)}",
        trail or "▁",
        "",
        f"**Cash out now** {theme.chips(value)}",
        f"**Stake** {theme.chips(game_round.stake)}",
    ]
    if status:
        lines += ["", status]
    return theme.embed(
        title=f"{theme.ICON['crash']} Crash",
        description="\n".join(lines),
        color=theme.TABLE,
        author=user,
        footer=rounds.fairness_footer(fairness),
    )


class CrashView(CasinoView):
    """One button, one decision, and a clock that will not wait for you."""

    def __init__(
        self,
        bot: Any,
        user: discord.abc.User,
        wager: Any,
        game_round: crash_game.Round,
        fairness: Fairness,
        play: PlayCallback,
    ) -> None:
        super().__init__(owner_id=user.id, timeout=crash_game.MAX_TICKS * crash_game.TICK_SECONDS + 30)
        self.bot = bot
        self.user = user
        self.wager = wager
        self.round = game_round
        self.fairness = fairness
        self.play = play
        self._lock = asyncio.Lock()
        self._done = asyncio.Event()
        self._settled = False
        self.task: asyncio.Task | None = None

    @discord.ui.button(label="Cash out", emoji="💰", style=discord.ButtonStyle.success)
    async def cash_out(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self._lock:
            if self.round.finished:
                raise RoundFinished()
            multiplier = self.round.cash_out()
        await interaction.response.defer()
        self._done.set()
        await self._settle(f"{theme.ICON['ok']} Cashed out at **{theme.multiplier(multiplier)}**.")

    async def run(self) -> None:
        """Tick the multiplier until it crashes or the player jumps off."""
        try:
            while True:
                try:
                    await asyncio.wait_for(self._done.wait(), timeout=crash_game.TICK_SECONDS)
                    return  # cashed out; _settle already ran
                except asyncio.TimeoutError:
                    pass
                async with self._lock:
                    if self.round.finished:
                        break
                    self.round.advance()
                    crashed = self.round.crashed or self.round.tick >= crash_game.MAX_TICKS
                if crashed:
                    break
                if self.message is not None:
                    try:
                        await self.message.edit(
                            embed=crash_embed(self.round, user=self.user, fairness=self.fairness),
                            view=self,
                        )
                    except discord.HTTPException:
                        break
            if self.round.cashed_at is None:
                await self._settle(
                    f"{theme.ICON['lose']} Crashed at **{theme.multiplier(self.round.crash_at)}**."
                )
        finally:
            self.stop()

    async def _settle(self, note: str) -> None:
        # Belt and braces: the ticker and the cash-out button both end the
        # round, and only one of them may pay it out.
        if self._settled:
            return
        self._settled = True
        payout = self.round.payout()
        result = await conclude(
            self.bot, self.user, self.wager, payout,
            note=f"crash {self.round.crash_at:.2f}x",
            guild=self.message.guild if self.message else None,
        )
        embed = result_embed(
            game="Crash",
            icon=theme.ICON["crash"],
            headline=f"crashed at {theme.multiplier(self.round.crash_at)}",
            body=note,
            result=result,
            fairness=self.fairness,
            user=self.user,
        )
        replay = ReplayView(owner_id=self.user.id, stake=self.wager.stake, play=self.play)
        if self.message is not None:
            try:
                await self.message.edit(embed=embed, view=replay)
                replay.message = self.message
            except discord.HTTPException:
                pass


# --------------------------------------------------------------------------
# Roulette bet board
# --------------------------------------------------------------------------


class RouletteBoardView(CasinoView):
    """Point at a bet instead of remembering its name.

    The old flow was ``!roulette 100 col2`` plus a help command to look up
    what ``col2`` meant. Here the table is the interface, and the typed form
    still works for anyone who prefers it.
    """

    def __init__(self, *, owner_id: int, play: Callable[[discord.Interaction, str, str], Awaitable[Any]], stake_hint: str = "500") -> None:
        super().__init__(owner_id=owner_id, timeout=300)
        self.play = play
        self.stake_hint = stake_hint
        self.add_item(self._group_select())

    def _group_select(self) -> discord.ui.Select:
        options = [
            discord.SelectOption(label=bet.label, value=bet.key, description=f"pays {bet.payout}:1")
            for bet in roulette_game.OUTSIDE_BETS
            if bet.group in ("dozen", "column")
        ]
        select: discord.ui.Select = discord.ui.Select(
            placeholder="Dozens and columns (pays 2:1)", options=options, row=2
        )

        async def callback(interaction: discord.Interaction) -> None:
            await self._prompt(interaction, select.values[0])

        select.callback = callback  # type: ignore[method-assign]
        return select

    async def _prompt(self, interaction: discord.Interaction, bet_key: str) -> None:
        bet = roulette_game.parse_bet(bet_key)
        label = bet.label if bet else bet_key

        async def handler(modal_interaction: discord.Interaction, raw: str) -> None:
            await self.play(modal_interaction, raw, bet_key)

        await interaction.response.send_modal(
            AmountModal(f"Roulette · {label}", handler, default=self.stake_hint)
        )

    @discord.ui.button(label="Red", emoji="🔴", style=discord.ButtonStyle.danger, row=0)
    async def red(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._prompt(interaction, "red")

    @discord.ui.button(label="Black", emoji="⚫", style=discord.ButtonStyle.secondary, row=0)
    async def black(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._prompt(interaction, "black")

    @discord.ui.button(label="Odd", style=discord.ButtonStyle.primary, row=0)
    async def odd(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._prompt(interaction, "odd")

    @discord.ui.button(label="Even", style=discord.ButtonStyle.primary, row=0)
    async def even(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._prompt(interaction, "even")

    @discord.ui.button(label="1-18", style=discord.ButtonStyle.primary, row=1)
    async def low(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._prompt(interaction, "low")

    @discord.ui.button(label="19-36", style=discord.ButtonStyle.primary, row=1)
    async def high(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._prompt(interaction, "high")

    @discord.ui.button(label="Single number (35:1)", emoji="🎯", style=discord.ButtonStyle.success, row=1)
    async def straight(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(StraightUpModal(self.play, self.stake_hint))


class StraightUpModal(discord.ui.Modal, title="Roulette · single number"):
    number: discord.ui.TextInput = discord.ui.TextInput(
        label="Number (0-36)", placeholder="17", max_length=2
    )
    stake: discord.ui.TextInput = discord.ui.TextInput(
        label="Stake", placeholder="e.g. 500, 10k, half, all", max_length=20
    )

    def __init__(self, play: Callable[[discord.Interaction, str, str], Awaitable[Any]], stake_hint: str) -> None:
        super().__init__(timeout=300)
        self.play = play
        self.stake.default = stake_hint

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.play(interaction, str(self.stake.value), str(self.number.value).strip())
