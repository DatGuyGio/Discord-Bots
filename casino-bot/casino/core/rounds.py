"""Everything that happens *after* a round is priced.

Settling a bet, awarding XP, checking achievements and writing the casino log
used to be copy-pasted at the end of every game handler — eleven times, with
small differences each time (some awarded XP, some did not; some checked
achievements, some did not; the roulette button flow checked achievements but
the roulette text command announced them in a separate message).

One function now closes every round, so a new game gets all of it for free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import discord

from casino.db.economy import Settlement, Wager
from casino.db.progression import Achievement
from casino.ui import theme

log = logging.getLogger(__name__)

#: A win bigger than this multiple of the stake gets announced in the casino
#: log channel, so staff can see anything unusual without reading every spin.
NOTABLE_MULTIPLE = 25


@dataclass(slots=True)
class RoundResult:
    """The settled round plus everything worth telling the player about."""

    settlement: Settlement
    level: int
    xp: int
    levelled_up: bool
    unlocked: list[Achievement] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def net(self) -> int:
        return self.settlement.net

    @property
    def balance(self) -> int:
        return self.settlement.balance

    @property
    def stake(self) -> int:
        return self.settlement.stake


async def conclude(
    bot: Any,
    user: discord.abc.User,
    wager: Wager,
    payout: int,
    *,
    outcome: str | None = None,
    note: str | None = None,
    guild: discord.Guild | None = None,
) -> RoundResult:
    """Credit the payout, then apply every consequence of the round."""
    settlement = await bot.economy.settle(wager, payout, outcome=outcome, note=note)

    settings = await bot.guilds_store.settings(wager.guild_id)
    xp_gain = await bot.progression.xp_for_round(settings, settlement.stake, settlement.won)
    level, xp, levelled_up = await bot.progression.award_xp(wager.guild_id, wager.user_id, xp_gain)

    profile = await bot.economy.profile(wager.guild_id, wager.user_id)
    profile["daily_streak"] = profile.get("daily_streak", 0)
    unlocked = await bot.progression.evaluate(wager.guild_id, wager.user_id, profile)

    if guild is not None and settlement.payout >= settlement.stake * NOTABLE_MULTIPLE and settlement.stake > 0:
        await bot.audit(
            guild,
            "casino",
            theme.embed(
                title=f"{theme.ICON['gem']} Notable win",
                description=(
                    f"{user.mention} won {theme.chips(settlement.net)} on "
                    f"**{settlement.game.title()}** from a {theme.chips(settlement.stake)} stake."
                ),
                color=theme.GOLD,
                timestamp=True,
            ),
        )

    return RoundResult(
        settlement=settlement, level=level, xp=xp, levelled_up=levelled_up, unlocked=list(unlocked)
    )


def decorate(embed: discord.Embed, result: RoundResult) -> discord.Embed:
    """Append level-ups and fresh achievements to a result embed."""
    if result.levelled_up:
        embed.add_field(
            name=f"{theme.ICON['level']} Level up",
            value=f"You are now **level {result.level}**.",
            inline=False,
        )
    if result.unlocked:
        embed.add_field(
            name=f"{theme.ICON['trophy']} Achievement unlocked",
            value="\n".join(f"{a.icon} **{a.name}** — {a.description}" for a in result.unlocked),
            inline=False,
        )
    return embed


def fairness_footer(fairness: Any) -> str:
    """The commitment line shown under every game result."""
    return f"Provably fair · commit {fairness.short_commitment} · /verify to check"
