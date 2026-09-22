"""The single gate every bet passes through.

The old bot had this logic three times over: once in ``parse_bet`` for text
commands, once inline in each ``play_*_ui`` coroutine for button and modal
flows, and once more in the blackjack join modal. The copies drifted, and the
button copies never checked the configured **maximum** bet — which is how a
999,999,999,999,999,999,999,999,999 chip blackjack hand got into the saved
data.

Now there is one funnel. :meth:`RoundGuard.open` performs every check in a
fixed order and returns a :class:`~casino.db.economy.Wager` with the stake
already debited, or raises a :class:`~casino.core.errors.CasinoError` that
the framework renders. A new game cannot forget a check, because the only way
to take a bet is to come through here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from casino.core.amounts import parse_amount
from casino.core.errors import (
    BetOutOfRange,
    CasinoBanned,
    CasinoError,
    GameDisabled,
    InsufficientFunds,
    Maintenance,
    SelfExcluded,
    WagerLimitReached,
)
from casino.db.economy import Economy, Wager, parse_iso
from casino.db.guilds import GuildStore
from casino.db.progression import Progression


class RoundGuard:
    def __init__(self, economy: Economy, guilds: GuildStore, progression: Progression) -> None:
        self.economy = economy
        self.guilds = guilds
        self.progression = progression

    # -- individual checks -------------------------------------------------

    async def ensure_player(self, guild_id: int | None, user_id: int) -> dict[str, Any]:
        settings = await self.guilds.settings(guild_id)
        if guild_id is not None:
            await self.economy.ensure(guild_id, user_id, int(settings["starting_balance"]))
        return settings

    async def check_open(self, guild_id: int | None, user_id: int) -> None:
        """Is the casino open to this player at all?"""
        if guild_id is None:
            return
        settings = await self.guilds.settings(guild_id)
        if settings.get("maintenance"):
            raise Maintenance()

        state = await self.progression.get(guild_id, user_id)
        now = datetime.now(timezone.utc)

        banned_until = parse_iso(state.get("casino_ban_until"))
        if banned_until and banned_until > now:
            raise CasinoBanned(
                f"An admin has blocked your casino access until "
                f"<t:{int(banned_until.timestamp())}:f> (<t:{int(banned_until.timestamp())}:R>)."
            )

        excluded_until = parse_iso(state.get("self_exclude_until"))
        if excluded_until and excluded_until > now:
            raise SelfExcluded(
                f"You self-excluded until <t:{int(excluded_until.timestamp())}:f>. "
                "This cannot be lifted early, by you or by staff. Take care of yourself."
            )

        limit = state.get("daily_wager_limit")
        if limit:
            midnight = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
            wagered = await self.economy.wagered_since(guild_id, user_id, midnight)
            if wagered >= int(limit):
                raise WagerLimitReached(
                    f"You have reached the daily wager limit you set for yourself "
                    f"(**{int(limit):,}** chips; you have staked **{wagered:,}** today). "
                    "It resets at midnight UTC."
                )

    async def check_game(self, guild_id: int | None, game: str) -> None:
        if not await self.guilds.game_enabled(guild_id, game):
            raise GameDisabled(game)

    async def resolve_stake(
        self, guild_id: int | None, user_id: int, raw: str | int, *, minimum: int | None = None
    ) -> int:
        """Parse and range-check a stake against the guild's limits."""
        settings = await self.guilds.settings(guild_id)
        balance = await self.economy.balance(guild_id, user_id) if guild_id else 0
        stake = parse_amount(raw, balance=balance)

        floor = int(minimum if minimum is not None else settings["min_bet"])
        ceiling = int(settings["max_bet"])
        if stake < floor or stake > ceiling:
            raise BetOutOfRange(stake, floor, ceiling)
        if stake > balance:
            raise InsufficientFunds(balance, stake)
        return stake

    # -- the funnel --------------------------------------------------------

    async def open(
        self,
        guild_id: int | None,
        user_id: int,
        game: str,
        raw_stake: str | int,
        *,
        detail: str | None = None,
    ) -> Wager:
        """Validate everything, then atomically take the stake.

        Order matters: availability first (so a self-excluded player is told
        why rather than being told their bet is too small), then the game
        switch, then the amount, then the debit.
        """
        if guild_id is None:
            raise CasinoError("The casino only runs inside a server.")
        await self.ensure_player(guild_id, user_id)
        await self.check_open(guild_id, user_id)
        await self.check_game(guild_id, game)
        stake = await self.resolve_stake(guild_id, user_id, raw_stake)
        return await self.economy.place_wager(guild_id, user_id, stake, game, detail)

    async def rtp(self, guild_id: int | None) -> float:
        settings = await self.guilds.settings(guild_id)
        return float(settings["rtp"])

    async def time_until_daily(self, guild_id: int, user_id: int) -> timedelta | None:
        """Remaining time on the daily, or ``None`` when it is ready."""
        profile = await self.economy.profile(guild_id, user_id)
        today = datetime.now(timezone.utc).date()
        if profile.get("last_daily_date") != today.isoformat():
            return None
        tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        return tomorrow - datetime.now(timezone.utc)

    async def time_until_work(self, guild_id: int, user_id: int) -> timedelta | None:
        settings = await self.guilds.settings(guild_id)
        profile = await self.economy.profile(guild_id, user_id)
        last = parse_iso(profile.get("last_work"))
        if last is None:
            return None
        cooldown = timedelta(seconds=float(settings["work_cooldown"]))
        remaining = cooldown - (datetime.now(timezone.utc) - last)
        return remaining if remaining.total_seconds() > 0 else None
