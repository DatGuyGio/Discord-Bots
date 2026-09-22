"""Blackjack: one message, real buttons, and the full set of moves.

What playing a hand used to look like: type ``!blackjack 500``, wait up to
twenty seconds for a lobby timer, then type ``!hit`` in chat and watch the bot
post a fresh message for every card. Only one hand could run per channel at a
time, and the only moves were hit, stand and double.

Now: ``/blackjack 500`` deals immediately into a single message you press
buttons on, with double, split and insurance available when the rules allow
them. Several people can have hands going in the same channel at once,
because each hand is its own message with its own shoe.
"""

from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from casino.core import rounds
from casino.core.errors import CasinoError, RoundFinished
from casino.core.rounds import conclude
from casino.games import blackjack as bj
from casino.games.cards import DECKS_IN_SHOE, Shoe, describe_total, render_hand
from casino.games.rng import Fairness
from casino.db.economy import merge_wagers
from casino.ui import theme
from casino.ui.games import ReplayView
from casino.ui.responder import Responder, Source
from casino.ui.views import CasinoView

#: Long enough to think, short enough that an abandoned hand settles itself
#: rather than leaving the stake debited forever.
TURN_TIMEOUT = 150


class TableHand:
    """Pairs a logical hand with the wager that funds it."""

    def __init__(self, logical: bj.Hand, wager: Any) -> None:
        self.logical = logical
        self.wager = wager
        self.outcome: bj.Outcome | None = None
        self.payout = 0


class BlackjackTable(CasinoView):
    """The interactive table: owns the shoe, the seat, and its own message."""

    def __init__(self, bot: Any, user: discord.abc.User, wager: Any, fairness: Fairness, play: Any) -> None:
        super().__init__(owner_id=user.id, timeout=TURN_TIMEOUT)
        self.bot = bot
        self.user = user
        self.guild_id = wager.guild_id
        self.fairness = fairness
        self.play = play
        self.shoe = Shoe(fairness)
        self.seat = bj.Seat(user_id=user.id)
        self.hands: list[TableHand] = []
        self.insurance_wager: Any = None
        self.insurance_offered = False
        self.finished = False
        self._lock = asyncio.Lock()

        opening = bj.Hand(cards=self.shoe.deal(2), bet=wager.stake)
        self.seat.hands.append(opening)
        self.hands.append(TableHand(opening, wager))
        self.dealer = self.shoe.deal(2)

    # -- state -------------------------------------------------------------

    @property
    def active_hand(self) -> TableHand:
        return self.hands[self.seat.active]

    @property
    def dealer_shows_ace(self) -> bool:
        return self.dealer[0].is_ace

    def _sync_buttons(self) -> None:
        hand = self.seat.current
        playable = not self.finished
        self.hit.disabled = not playable or hand.finished
        self.stand.disabled = not playable or hand.finished
        self.double.disabled = not playable or not hand.can_double
        self.split.disabled = not playable or not hand.can_split(len(self.seat.hands))
        self.insurance.disabled = not (
            playable
            and self.dealer_shows_ace
            and not self.insurance_offered
            and len(self.seat.hands) == 1
            and len(hand.cards) == 2
        )

    # -- rendering ---------------------------------------------------------

    def embed(self, *, reveal: bool = False, note: str | None = None) -> discord.Embed:
        dealer_line = (
            f"{render_hand(self.dealer)} → {describe_total(self.dealer)}"
            if reveal
            else f"{render_hand(self.dealer, hide_from=1)} → **{self.dealer[0].value}** + hole card"
        )
        lines = [f"**Dealer**\n{dealer_line}", ""]
        for index, hand in enumerate(self.hands):
            marker = "▶ " if (not self.finished and index == self.seat.active) else ""
            tags = []
            if hand.logical.doubled:
                tags.append("doubled")
            if hand.logical.from_split:
                tags.append("split")
            if hand.outcome is not None:
                tags.append(f"**{hand.outcome.label}**")
            suffix = f" · {' · '.join(tags)}" if tags else ""
            label = f"Hand {index + 1}" if len(self.hands) > 1 else "Your hand"
            lines.append(
                f"{marker}**{label}** ({hand.logical.bet:,} chips){suffix}\n"
                f"{render_hand(hand.logical.cards)} → {describe_total(hand.logical.cards)}"
            )
        if self.insurance_wager is not None:
            lines.append(f"\n{theme.ICON['shield']} Insurance {theme.chips(self.insurance_wager.stake)}")
        if note:
            lines.append(f"\n{note}")

        return theme.embed(
            title=f"{theme.ICON['blackjack']} Blackjack",
            description="\n".join(lines),
            color=theme.TABLE if not self.finished else theme.BRAND,
            author=self.user,
            footer=rounds.fairness_footer(self.fairness),
        )

    # -- lifecycle ---------------------------------------------------------

    async def start(self, responder: Responder) -> None:
        """Deal, then either settle a natural at once or hand over control."""
        self._sync_buttons()
        immediate = self.seat.current.natural or bj.is_blackjack(self.dealer)
        await responder.send(embed=self.embed(reveal=immediate), view=None if immediate else self)
        self.message = responder.message
        if immediate:
            note = (
                "Blackjack!" if self.seat.current.natural else "The dealer has blackjack."
            )
            await self._settle(note=note)

    async def _refresh(self, interaction: discord.Interaction, note: str | None = None) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(note=note), view=self)

    async def _advance(self, interaction: discord.Interaction, note: str | None = None) -> None:
        """Move to the next unfinished hand, or settle if the seat is done."""
        if self.seat.advance() and not self.seat.current.finished:
            return await self._refresh(interaction, note)
        await interaction.response.defer()
        await self._settle(note=note)

    async def on_timeout(self) -> None:
        """An abandoned hand stands itself and settles.

        This matters more than it looks: the stake is already debited when the
        hand is dealt, so a table that simply stopped responding would quietly
        delete the player's chips.
        """
        if self.finished:
            return
        for hand in self.seat.hands:
            if not hand.finished:
                hand.stood = True
        await self._settle(note="⏱️ Timed out — the hand was stood automatically.")

    # -- settlement --------------------------------------------------------

    async def _settle(self, *, note: str | None = None) -> None:
        async with self._lock:
            if self.finished:
                return
            self.finished = True
        self.disable_all()

        # The dealer only draws if something can still beat them.
        live = any(not hand.logical.busted and not hand.logical.natural for hand in self.hands)
        if live and not bj.is_blackjack(self.dealer):
            bj.play_dealer(self.dealer, self.shoe)

        insured, insurance_payout = bj.insurance_result(
            self.dealer, self.insurance_wager.stake if self.insurance_wager else 0
        )
        results: list[rounds.RoundResult] = []
        if self.insurance_wager is not None:
            results.append(
                await conclude(
                    self.bot, self.user, self.insurance_wager, insurance_payout,
                    outcome="win" if insured else "lose", note="insurance",
                )
            )

        for hand in self.hands:
            outcome, payout = bj.resolve(hand.logical, self.dealer)
            hand.outcome = outcome
            hand.payout = payout
            results.append(
                await conclude(
                    self.bot, self.user, hand.wager, payout,
                    outcome=(
                        "win" if payout > hand.logical.bet
                        else "push" if payout == hand.logical.bet
                        else "lose"
                    ),
                    note=outcome.value,
                )
            )

        total_stake = sum(hand.logical.bet for hand in self.hands) + (
            self.insurance_wager.stake if self.insurance_wager else 0
        )
        total_return = sum(hand.payout for hand in self.hands) + insurance_payout
        net = total_return - total_stake
        balance = results[-1].balance if results else await self.bot.economy.balance(self.guild_id, self.user.id)

        summary_parts = []
        for index, hand in enumerate(self.hands):
            if hand.outcome is None:
                continue
            prefix = f"Hand {index + 1}: " if len(self.hands) > 1 else ""
            summary_parts.append(f"{prefix}**{hand.outcome.label}**")
        if self.insurance_wager is not None:
            summary_parts.append(f"Insurance {'paid 2:1' if insured else 'lost'}")

        embed = self.embed(reveal=True, note=note)
        embed.title = f"{theme.ICON['blackjack']} Blackjack · {theme.outcome_icon(net)}"
        embed.color = theme.outcome_color(net)
        embed.add_field(name="Result", value=" · ".join(summary_parts) or "—", inline=False)
        embed.add_field(name="Settlement", value=theme.result_block(total_stake, net, balance), inline=False)
        for result in results:
            rounds.decorate(embed, result)

        view = ReplayView(owner_id=self.user.id, stake=self.hands[0].logical.bet, play=self.play)
        view.message = self.message
        if self.message is not None:
            try:
                await self.message.edit(embed=embed, view=view)
            except discord.HTTPException:
                pass
        self.stop()

    # -- buttons -----------------------------------------------------------

    @discord.ui.button(label="Hit", emoji="🃏", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished:
            raise RoundFinished()
        card = self.seat.hit(self.shoe)
        hand = self.seat.current
        if hand.busted:
            return await self._advance(interaction, note=f"Drew {card.render()} and busted on **{hand.total}**.")
        if hand.total == 21:
            hand.stood = True
            return await self._advance(interaction, note=f"Drew {card.render()} for **21**.")
        await self._refresh(interaction, note=f"Drew {card.render()}.")

    @discord.ui.button(label="Stand", emoji="✋", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished:
            raise RoundFinished()
        self.seat.current.stood = True
        await self._advance(interaction, note=f"Standing on **{self.seat.current.total}**.")

    @discord.ui.button(label="Double", emoji="⏫", style=discord.ButtonStyle.success)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished:
            raise RoundFinished()
        hand = self.seat.current
        # Take the extra stake first: if the player cannot cover it they are
        # told before a card is dealt, and the card is never wasted.
        top_up = await self.bot.economy.place_wager(self.guild_id, self.user.id, hand.bet, "blackjack")
        merge_wagers(self.active_hand.wager, top_up)
        self.seat.double(self.shoe)
        await self._advance(
            interaction,
            note=f"Doubled to **{hand.bet:,}** and drew {hand.cards[-1].render()} for **{hand.total}**.",
        )

    @discord.ui.button(label="Split", emoji="🔀", style=discord.ButtonStyle.success)
    async def split(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished:
            raise RoundFinished()
        hand = self.seat.current
        second = await self.bot.economy.place_wager(self.guild_id, self.user.id, hand.bet, "blackjack")
        if not self.seat.split(self.shoe):
            await self.bot.economy.refund(second, "split not allowed")
            raise CasinoError("That hand cannot be split.")
        self.hands.append(TableHand(self.seat.hands[-1], second))
        if self.seat.current.finished:
            return await self._advance(interaction, note="Split aces take one card each and stand.")
        await self._refresh(interaction, note="Split into two hands.")

    @discord.ui.button(label="Insurance", emoji="🛡️", style=discord.ButtonStyle.secondary)
    async def insurance(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.finished:
            raise RoundFinished()
        self.insurance_offered = True
        half = max(1, self.hands[0].logical.bet // 2)
        self.insurance_wager = await self.bot.economy.place_wager(
            self.guild_id, self.user.id, half, "blackjack"
        )
        await self._refresh(
            interaction,
            note=f"Insurance taken for {theme.chips(half)} — pays 2:1 if the dealer has blackjack.",
        )


class Blackjack(commands.Cog, name="Blackjack"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @commands.hybrid_command(name="blackjack", aliases=["bj"], description="Play a hand of blackjack.")
    @app_commands.describe(stake="How much to bet. Accepts 500, 10k, half, all.")
    async def blackjack_command(self, ctx: commands.Context, stake: str) -> None:
        await self.play_hand(ctx, stake)

    async def play_hand(self, source: Source, stake: str | int) -> None:
        responder = Responder(source)
        wager = await self.bot.guard.open(responder.guild_id, responder.user.id, "blackjack", stake)
        fairness = Fairness(client_seed=str(responder.user.id))

        async def play(interaction: discord.Interaction, amount: int) -> None:
            await self.play_hand(interaction, amount)

        table = BlackjackTable(self.bot, responder.user, wager, fairness, play)
        await table.start(responder)

    @commands.hybrid_command(name="blackjackrules", aliases=["bjhelp"], description="House rules for blackjack.")
    async def rules(self, ctx: commands.Context) -> None:
        embed = theme.embed(
            title=f"{theme.ICON['blackjack']} Blackjack house rules",
            description=theme.bullet_list(
                [
                    f"**{DECKS_IN_SHOE}-deck shoe**, reshuffled before it runs low.",
                    "**Dealer stands on all 17s**, including soft 17.",
                    "**Blackjack pays 3:2**, and beats a dealer 21 made from three or more cards.",
                    "**Double** on any first two cards; you then get exactly one more card.",
                    "**Split** a pair once. Split aces take one card each and stand.",
                    "**Insurance** is offered when the dealer shows an ace, and pays 2:1.",
                    "Equal totals **push** and your stake comes back.",
                ]
            ),
            color=theme.TABLE,
            footer="Roughly 99.5% return to player with basic strategy — the best odds in the house.",
        )
        await ctx.send(embed=embed)


async def setup(bot: Any) -> None:
    await bot.add_cog(Blackjack(bot))
