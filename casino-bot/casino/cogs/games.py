"""The house games: roulette, slots, coinflip, dice, mines, crash, and duels.

Every game follows the same four steps, and the shared helpers make that
literal rather than aspirational:

1. ``guard.open(...)`` validates and atomically takes the stake.
2. The pure logic in ``casino.games.*`` decides the outcome from a seeded,
   committed random stream.
3. ``rounds.conclude(...)`` credits the payout and applies XP, achievements
   and logging in one transaction.
4. A result embed goes out with replay controls attached.

If step 1 raises, no chips have moved. If step 2 raises, step 3 never runs and
the stake is refunded by the caller. There is no path that debits without
eventually settling or refunding.
"""

from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from casino.core import rounds
from casino.core.errors import CasinoError
from casino.core.rounds import conclude
from casino.games import crash as crash_game
from casino.games import mines as mines_game
from casino.games import roulette as roulette_game
from casino.games import slots as slots_game
from casino.games.payouts import fair_multiplier
from casino.games.rng import Fairness, system_random
from casino.ui import theme
from casino.ui.games import (
    CrashView,
    MinesView,
    ReplayView,
    RouletteBoardView,
    crash_embed,
    mines_embed,
    result_embed,
)
from casino.ui.responder import Responder, Source
from casino.ui.views import CasinoView

SPIN_FRAMES = 6


class Games(commands.Cog, name="Games"):
    """House games. The dealer is the bot; the chips are not real."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _fairness(self, responder: Responder, nonce: int = 0) -> Fairness:
        """Fresh seed material, bound to the player, for one round."""
        return Fairness(client_seed=str(responder.user.id), nonce=nonce)

    async def _open(self, responder: Responder, game: str, stake: str | int, detail: str | None = None) -> Any:
        return await self.bot.guard.open(
            responder.guild_id, responder.user.id, game, stake, detail=detail
        )

    def _replay(self, responder: Responder, handler: Any, stake: int, **extra: Any) -> ReplayView:
        async def play(interaction: discord.Interaction, amount: int) -> None:
            await handler(interaction, amount, **extra)

        return ReplayView(owner_id=responder.user.id, stake=stake, play=play)

    # ==================================================================
    # Roulette
    # ==================================================================

    @commands.hybrid_command(name="roulette", aliases=["r"], description="Spin the roulette wheel.")
    @app_commands.describe(
        stake="How much to bet. Accepts 500, 10k, half, all.",
        bet="red, black, odd, even, low, high, dozen1-3, column1-3, or a number 0-36.",
    )
    async def roulette_command(self, ctx: commands.Context, stake: str | None = None, bet: str | None = None) -> None:
        responder = Responder(ctx)
        if stake is None or bet is None:
            return await self._roulette_board(responder)
        await self.play_roulette(ctx, stake, bet)

    async def _roulette_board(self, responder: Responder) -> None:
        """No arguments: show the table and let them point at a bet."""
        hint = await self._stake_hint(responder)
        view = RouletteBoardView(owner_id=responder.user.id, play=self.play_roulette, stake_hint=hint)
        lines = roulette_game.bet_help_lines()
        await responder.send(
            embed=theme.embed(
                title=f"{theme.ICON['roulette']} Roulette",
                description="Choose a bet, then enter your stake.\n\n" + "\n".join(lines),
                color=theme.TABLE,
                author=responder.user,
                footer="European wheel · single zero · 2.70% house edge on every bet",
            ),
            view=view,
        )

    async def play_roulette(self, source: Source, stake: str | int, bet: str) -> None:
        """Resolve one roulette bet. Shared by the command, board and replays."""
        responder = Responder(source)
        kind = roulette_game.parse_bet(bet)
        if kind is None:
            raise CasinoError(
                f"`{bet}` is not a bet on this table. Try `red`, `black`, `odd`, `even`, "
                "`low`, `high`, `dozen2`, `column3`, or a number from 0 to 36."
            )

        wager = await self._open(responder, "roulette", stake, detail=kind.key)
        fairness = self._fairness(responder)
        pocket = fairness.below(len(roulette_game.POCKETS))
        outcome = roulette_game.spin(kind, wager.stake, pocket)

        await responder.send(
            embed=theme.embed(
                title=f"{theme.ICON['roulette']} Roulette",
                description=(
                    f"**{theme.chips(wager.stake)}** on **{kind.label}** (pays {kind.payout}:1)\n\n"
                    "The wheel is spinning…"
                ),
                color=theme.TABLE,
                author=responder.user,
            )
        )
        await asyncio.sleep(1.4)

        result = await conclude(
            self.bot, responder.user, wager, outcome.payout,
            note=kind.key, guild=responder.guild,
        )
        near = ", ".join(str(n) for n in roulette_game.neighbours(pocket, 1))
        embed = result_embed(
            game="Roulette",
            icon=theme.ICON["roulette"],
            headline=outcome.describe(),
            body=(
                f"**Your bet** {kind.label} (pays {kind.payout}:1)\n"
                f"**Neighbours** {near}"
            ),
            result=result,
            fairness=fairness,
            user=responder.user,
        )
        view = self._replay(responder, self.play_roulette_amount, wager.stake, bet=kind.key)
        await responder.edit(embed=embed, view=view)

    async def play_roulette_amount(self, interaction: discord.Interaction, amount: int, bet: str = "red") -> None:
        await self.play_roulette(interaction, amount, bet)

    # ==================================================================
    # Slots
    # ==================================================================

    @commands.hybrid_command(name="slots", aliases=["slot", "spin"], description="Spin the slot machine.")
    @app_commands.describe(stake="How much to bet. Accepts 500, 10k, half, all.")
    async def slots_command(self, ctx: commands.Context, stake: str | None = None) -> None:
        await self.play_slots(ctx, stake or await self._stake_hint(Responder(ctx)))

    async def play_slots(self, source: Source, stake: str | int) -> None:
        responder = Responder(source)
        wager = await self._open(responder, "slots", stake)
        fairness = self._fairness(responder)
        spin = slots_game.spin(wager.stake, fairness)

        # Animate by editing one message. Each frame shows the progress of the
        # reels coming to rest, which reads as motion in Discord.
        await responder.send(
            embed=theme.embed(
                title=f"{theme.ICON['slots']} Slots",
                description=f"`▱▱▱▱▱▱`\n# ⬛  ⬛  ⬛\n\n{theme.chips(wager.stake)} in play",
                color=theme.TABLE,
                author=responder.user,
            )
        )
        for frame in range(SPIN_FRAMES):
            reels = [system_random.choice(slots_game.REELS).emoji for _ in range(3)]
            # Lock the reels left to right so the last frame builds tension.
            locked = spin.reels[: max(0, frame - SPIN_FRAMES + 4)]
            display = [symbol.emoji for symbol in locked] + reels[len(locked) :]
            bar = "▰" * (frame + 1) + "▱" * (SPIN_FRAMES - frame - 1)
            await responder.edit(
                embed=theme.embed(
                    title=f"{theme.ICON['slots']} Slots",
                    description=f"`{bar}`\n# {'  '.join(display[:3])}\n\n{theme.chips(wager.stake)} in play",
                    color=theme.TABLE,
                    author=responder.user,
                )
            )
            await asyncio.sleep(0.45)

        result = await conclude(
            self.bot, responder.user, wager, spin.payout,
            note=spin.kind, guild=responder.guild,
        )
        body = f"# {spin.display}\n**{spin.headline}**"
        if spin.multiplier:
            body += f" · pays {theme.multiplier(spin.multiplier)}"
        embed = result_embed(
            game="Slots",
            icon="🎉" if spin.is_jackpot else theme.ICON["slots"],
            headline=spin.headline if spin.is_jackpot else ("win" if result.net > 0 else "no win"),
            body=body,
            result=result,
            fairness=fairness,
            user=responder.user,
        )
        await responder.edit(embed=embed, view=self._replay(responder, self.play_slots_amount, wager.stake))

    async def play_slots_amount(self, interaction: discord.Interaction, amount: int) -> None:
        await self.play_slots(interaction, amount)

    @commands.hybrid_command(name="paytable", description="Show the slots paytable and its return to player.")
    async def paytable(self, ctx: commands.Context) -> None:
        embed = theme.embed(
            title=f"{theme.ICON['slots']} Slots paytable",
            description="\n".join(slots_game.paytable_lines()),
            color=theme.GOLD,
            footer=(
                f"Return to player {slots_game.theoretical_rtp() * 100:.2f}% · "
                f"something pays on {slots_game.hit_rate() * 100:.1f}% of spins"
            ),
        )
        embed.add_field(
            name="How this is calculated",
            value=(
                "Exactly, not estimated: there are only 512 reel combinations, so the "
                "bot enumerates all of them and weights each by its probability."
            ),
            inline=False,
        )
        await ctx.send(embed=embed)

    # ==================================================================
    # Coinflip
    # ==================================================================

    @commands.hybrid_command(name="coinflip", aliases=["cf", "flip"], description="Flip a coin.")
    @app_commands.describe(stake="How much to bet.", side="Heads or tails.")
    @app_commands.choices(
        side=[
            app_commands.Choice(name="Heads", value="heads"),
            app_commands.Choice(name="Tails", value="tails"),
        ]
    )
    async def coinflip_command(self, ctx: commands.Context, stake: str, side: str = "heads") -> None:
        await self.play_coinflip(ctx, stake, side)

    async def play_coinflip(self, source: Source, stake: str | int, side: str = "heads") -> None:
        responder = Responder(source)
        choice = side.strip().lower()
        choice = "heads" if choice in ("h", "head", "heads") else "tails" if choice in ("t", "tail", "tails") else ""
        if not choice:
            raise CasinoError("Pick `heads` or `tails`.")

        wager = await self._open(responder, "coinflip", stake, detail=choice)
        fairness = self._fairness(responder)
        rtp = await self.bot.guard.rtp(responder.guild_id)
        landed = "heads" if fairness.float() < 0.5 else "tails"
        won = landed == choice
        payout = int(wager.stake * fair_multiplier(0.5, rtp)) if won else 0

        result = await conclude(
            self.bot, responder.user, wager, payout, note=landed, guild=responder.guild
        )
        embed = result_embed(
            game="Coinflip",
            icon="🪙",
            headline=landed.title(),
            body=f"You called **{choice}** and it landed **{landed}**.",
            result=result,
            fairness=fairness,
            user=responder.user,
        )
        await responder.send(
            embed=embed, view=self._replay(responder, self.play_coinflip_amount, wager.stake, side=choice)
        )

    async def play_coinflip_amount(self, interaction: discord.Interaction, amount: int, side: str = "heads") -> None:
        await self.play_coinflip(interaction, amount, side)

    # ==================================================================
    # Dice
    # ==================================================================

    @commands.hybrid_command(name="dice", aliases=["die", "roll"], description="Bet that a die clears a target.")
    @app_commands.describe(
        stake="How much to bet.",
        target="Win if the die rolls this number or higher (2-6). Longer odds pay more.",
    )
    async def dice_command(self, ctx: commands.Context, stake: str, target: int = 4) -> None:
        await self.play_dice(ctx, stake, target)

    async def play_dice(self, source: Source, stake: str | int, target: int = 4) -> None:
        """Roll-over dice.

        The old version had you guess an exact face and paid a flat 5:1 on a
        1-in-6 shot — a 100% return, so the game had no edge at all and the
        payout never changed with the risk. Here the target sets the odds and
        the payout follows from them.
        """
        responder = Responder(source)
        try:
            target = int(target)
        except (TypeError, ValueError):
            raise CasinoError("Pick a target from 2 to 6.") from None
        if not 2 <= target <= 6:
            raise CasinoError("Pick a target from 2 to 6. Higher targets are less likely and pay more.")

        chance = (7 - target) / 6
        rtp = await self.bot.guard.rtp(responder.guild_id)
        wager = await self._open(responder, "dice", stake, detail=f">={target}")
        fairness = self._fairness(responder)
        roll = fairness.integer(1, 6)
        won = roll >= target
        payout = int(wager.stake * fair_multiplier(chance, rtp)) if won else 0

        result = await conclude(
            self.bot, responder.user, wager, payout, note=f"rolled {roll}", guild=responder.guild
        )
        faces = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}
        embed = result_embed(
            game="Dice",
            icon=theme.ICON["dice"],
            headline=f"rolled {roll}",
            body=(
                f"# {faces[roll]} {roll}\n"
                f"**Target** {target} or higher · **chance** {theme.percent(chance)} · "
                f"**pays** {theme.multiplier(fair_multiplier(chance, rtp))}"
            ),
            result=result,
            fairness=fairness,
            user=responder.user,
        )
        await responder.send(
            embed=embed, view=self._replay(responder, self.play_dice_amount, wager.stake, target=target)
        )

    async def play_dice_amount(self, interaction: discord.Interaction, amount: int, target: int = 4) -> None:
        await self.play_dice(interaction, amount, target)

    # ==================================================================
    # Mines
    # ==================================================================

    @commands.hybrid_command(name="mines", description="Reveal tiles for a rising multiplier, then bank it.")
    @app_commands.describe(stake="How much to bet.", mines="How many bombs to hide (1-19). More bombs pay more.")
    async def mines_command(self, ctx: commands.Context, stake: str, mines: int = mines_game.DEFAULT_MINES) -> None:
        await self.play_mines(ctx, stake, mines)

    async def play_mines(self, source: Source, stake: str | int, mines: int = mines_game.DEFAULT_MINES) -> None:
        responder = Responder(source)
        try:
            count = int(mines)
        except (TypeError, ValueError):
            raise CasinoError(f"Choose between 1 and {mines_game.MAX_MINES} mines.") from None
        if not mines_game.MIN_MINES <= count <= mines_game.MAX_MINES:
            raise CasinoError(f"Choose between {mines_game.MIN_MINES} and {mines_game.MAX_MINES} mines.")

        rtp = await self.bot.guard.rtp(responder.guild_id)
        wager = await self._open(responder, "mines", stake, detail=f"{count} mines")
        fairness = self._fairness(responder)
        board = mines_game.Board.create(count, fairness, rtp)

        async def play(interaction: discord.Interaction, amount: int) -> None:
            await self.play_mines(interaction, amount, count)

        view = MinesView(self.bot, responder.user, wager, board, fairness, play)
        await responder.send(
            embed=mines_embed(board, wager.stake, user=responder.user, fairness=fairness), view=view
        )

    async def play_mines_amount(self, interaction: discord.Interaction, amount: int, mines: int = mines_game.DEFAULT_MINES) -> None:
        await self.play_mines(interaction, amount, mines)

    # ==================================================================
    # Crash
    # ==================================================================

    @commands.hybrid_command(name="crash", description="Ride a rising multiplier and cash out before it crashes.")
    @app_commands.describe(stake="How much to bet.")
    async def crash_command(self, ctx: commands.Context, stake: str) -> None:
        await self.play_crash(ctx, stake)

    async def play_crash(self, source: Source, stake: str | int) -> None:
        responder = Responder(source)
        rtp = await self.bot.guard.rtp(responder.guild_id)
        wager = await self._open(responder, "crash", stake)
        fairness = self._fairness(responder)
        game_round = crash_game.Round.create(wager.stake, fairness, rtp)

        async def play(interaction: discord.Interaction, amount: int) -> None:
            await self.play_crash(interaction, amount)

        view = CrashView(self.bot, responder.user, wager, game_round, fairness, play)
        await responder.send(
            embed=crash_embed(game_round, user=responder.user, fairness=fairness, status="Cash out any time."),
            view=view,
        )
        view.message = responder.message
        # The ride runs in the background so the command returns immediately
        # and the player can keep using the bot while the number climbs. The
        # task reference is kept on the view: asyncio only holds a weak
        # reference to running tasks, so a fire-and-forget task can be
        # garbage collected mid-flight and silently stop ticking.
        view.task = asyncio.create_task(view.run())

    # ==================================================================
    # Duel (player versus player, no house cut)
    # ==================================================================

    @commands.hybrid_command(name="duel", description="Challenge another player to a 50/50 for chips.")
    @app_commands.describe(opponent="Who to challenge.", stake="How much each side puts in.")
    async def duel_command(self, ctx: commands.Context, opponent: discord.Member, stake: str) -> None:
        responder = Responder(ctx)
        if opponent.id == responder.user.id:
            raise CasinoError("Duelling yourself is not a game.")
        if opponent.bot:
            raise CasinoError("Bots do not carry chips.")

        await self.bot.guard.check_open(responder.guild_id, responder.user.id)
        await self.bot.guard.check_game(responder.guild_id, "duel")
        amount = await self.bot.guard.resolve_stake(responder.guild_id, responder.user.id, stake)

        await self.bot.guard.ensure_player(responder.guild_id, opponent.id)
        opponent_balance = await self.bot.economy.balance(responder.guild_id, opponent.id)
        if opponent_balance < amount:
            raise CasinoError(
                f"{opponent.mention} has **{opponent_balance:,}** chips and cannot cover **{amount:,}**."
            )

        view = DuelView(self.bot, responder.user, opponent, amount)
        await responder.send(
            content=opponent.mention,
            embed=theme.embed(
                title=f"{theme.ICON['duel']} Duel challenge",
                description=(
                    f"{responder.user.mention} challenges {opponent.mention} for "
                    f"**{amount:,}** chips each.\n\n"
                    f"A coin decides it and the winner takes the whole "
                    f"**{amount * 2:,}** chip pot. The house takes nothing.\n\n"
                    f"{opponent.mention}, do you accept?"
                ),
                color=theme.GOLD,
            ),
            view=view,
        )

    # ------------------------------------------------------------------

    async def _stake_hint(self, responder: Responder) -> str:
        if responder.guild_id is None:
            return "100"
        await self.bot.guard.ensure_player(responder.guild_id, responder.user.id)
        profile = await self.bot.economy.profile(responder.guild_id, responder.user.id)
        settings = await self.bot.guilds_store.settings(responder.guild_id)
        last = int(profile.get("last_stake") or 0)
        return str(last if last >= int(settings["min_bet"]) else int(settings["min_bet"]))


class DuelView(CasinoView):
    """A challenge that only the named opponent can answer.

    Balances are re-checked at the moment of acceptance, and both stakes are
    debited before the coin is flipped, so neither side can spend their stake
    while the challenge is pending.
    """

    def __init__(self, bot: Any, challenger: discord.abc.User, opponent: discord.Member, stake: int) -> None:
        super().__init__(owner_id=None, timeout=90)
        self.bot = bot
        self.challenger = challenger
        self.opponent = opponent
        self.stake = stake
        self._resolved = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.opponent.id:
            return True
        if interaction.user.id == self.challenger.id:
            await interaction.response.send_message(
                embed=theme.error("You issued this duel — it is your opponent's call."), ephemeral=True
            )
            return False
        await interaction.response.send_message(
            embed=theme.error("This duel is between two other players."), ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        if self._resolved or self.message is None:
            return
        try:
            await self.message.edit(
                embed=theme.embed(
                    title=f"{theme.ICON['duel']} Duel expired",
                    description=f"{self.opponent.mention} did not answer. No chips moved.",
                    color=theme.PUSH,
                ),
                view=None,
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Accept", emoji="⚔️", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._resolved:
            return
        guild_id = interaction.guild_id
        # The opponent has to be allowed to gamble too: a self-excluded player
        # cannot be pulled back to the tables by someone else's challenge.
        await self.bot.guard.check_open(guild_id, self.opponent.id)
        self._resolved = True
        self.stop()

        challenger_wager = await self.bot.economy.place_wager(
            guild_id, self.challenger.id, self.stake, "duel"
        )
        try:
            opponent_wager = await self.bot.economy.place_wager(
                guild_id, self.opponent.id, self.stake, "duel"
            )
        except CasinoError:
            # Never leave one side's chips in limbo.
            await self.bot.economy.refund(challenger_wager, "duel declined: opponent short")
            return await interaction.response.edit_message(
                embed=theme.error("Your balance changed and no longer covers the duel. Nothing moved."),
                view=None,
            )

        fairness = Fairness(client_seed=f"{self.challenger.id}:{self.opponent.id}")
        challenger_wins = fairness.float() < 0.5
        winner, loser = (
            (self.challenger, self.opponent) if challenger_wins else (self.opponent, self.challenger)
        )
        winner_wager = challenger_wager if challenger_wins else opponent_wager
        loser_wager = opponent_wager if challenger_wins else challenger_wager

        # The pot is exactly the two stakes: the bot mints nothing here.
        await conclude(self.bot, winner, winner_wager, self.stake * 2, outcome="win", guild=interaction.guild)
        await conclude(self.bot, loser, loser_wager, 0, outcome="lose", guild=interaction.guild)

        await interaction.response.edit_message(
            embed=theme.embed(
                title=f"{theme.ICON['duel']} {winner.display_name} wins the duel",
                description=(
                    f"{self.challenger.mention} vs {self.opponent.mention} · "
                    f"**{self.stake:,}** each\n\n"
                    f"🏆 **{winner.display_name}** takes the **{self.stake * 2:,}** chip pot."
                ),
                color=theme.WIN,
                footer=rounds.fairness_footer(fairness),
            ),
            view=None,
        )

    @discord.ui.button(label="Decline", emoji="🚫", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._resolved = True
        self.stop()
        await interaction.response.edit_message(
            embed=theme.embed(
                title=f"{theme.ICON['duel']} Duel declined",
                description=f"{self.opponent.mention} passed. No chips moved.",
                color=theme.PUSH,
            ),
            view=None,
        )


async def setup(bot: Any) -> None:
    await bot.add_cog(Games(bot))
