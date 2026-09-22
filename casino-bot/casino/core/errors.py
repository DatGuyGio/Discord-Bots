"""Domain errors.

Every rejection a player can hit — too poor, bet too big, still on cooldown,
self-excluded — is one of these. The command framework catches
:class:`CasinoError` in a single place and renders it as a friendly embed, so
individual commands do not each re-implement "send a warning message and
return".
"""

from __future__ import annotations

from datetime import timedelta

from discord.ext import commands


class CasinoError(commands.CommandError):
    """Base class for anything we want to show the player verbatim."""

    title: str = "Hold on"
    ephemeral: bool = True

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InsufficientFunds(CasinoError):
    title = "Not enough chips"

    def __init__(self, balance: int, needed: int) -> None:
        self.balance = balance
        self.needed = needed
        super().__init__(
            f"That costs **{needed:,}** chips but you only have **{balance:,}**.\n"
            "Try `/work`, `/daily`, or a smaller stake."
        )


class BetOutOfRange(CasinoError):
    title = "Bet out of range"

    def __init__(self, stake: int, minimum: int, maximum: int) -> None:
        self.stake = stake
        super().__init__(
            f"**{stake:,}** is outside this server's limits "
            f"(**{minimum:,}** – **{maximum:,}** chips)."
        )


class InvalidAmount(CasinoError):
    title = "That is not an amount"

    def __init__(self, raw: str) -> None:
        super().__init__(
            f"I could not read `{raw}` as an amount. "
            "Use a whole number, or a shortcut like `all`, `half`, `1k`, `2.5m`."
        )


class OnCooldown(CasinoError):
    title = "Not ready yet"

    def __init__(self, what: str, remaining: timedelta) -> None:
        self.remaining = remaining
        seconds = max(1, int(remaining.total_seconds()))
        if seconds >= 3600:
            pretty = f"{seconds // 3600}h {seconds % 3600 // 60}m"
        elif seconds >= 60:
            pretty = f"{seconds // 60}m {seconds % 60}s"
        else:
            pretty = f"{seconds}s"
        super().__init__(f"Your **{what}** is ready again in **{pretty}**.")


class GameDisabled(CasinoError):
    title = "Game unavailable"

    def __init__(self, game: str) -> None:
        super().__init__(f"**{game.title()}** has been switched off by the server admins.")


class Maintenance(CasinoError):
    title = "Casino closed"

    def __init__(self) -> None:
        super().__init__("The casino is in maintenance mode. Please try again shortly.")


class CasinoBanned(CasinoError):
    title = "Casino access blocked"


class SelfExcluded(CasinoError):
    title = "Self-exclusion active"


class WagerLimitReached(CasinoError):
    title = "Daily limit reached"


class NotPermitted(CasinoError):
    title = "Not allowed"


class RoundFinished(CasinoError):
    title = "Round already over"

    def __init__(self) -> None:
        super().__init__("That round has already finished.")
