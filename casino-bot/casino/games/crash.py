"""Crash: a multiplier climbs, and you decide when to jump off.

New game, and the most visually alive thing the bot does — one message that
edits itself every beat while the number climbs, with a single cash-out
button.

The maths is the classic one. For a target return-to-player ``r``, the crash
point is distributed so that ``P(crash >= x) = r / x``. Cashing out at ``x``
therefore has expected value ``x * (r / x) = r`` no matter which target you
pick, so there is no "best" strategy to reverse-engineer — only a choice of
variance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from casino.config import DEFAULT_RTP
from casino.games.rng import Fairness

#: Multiplier at each tick. Slow at first so early cash-outs feel deliberate,
#: then accelerating, which is what makes the game tense.
TICK_SECONDS = 1.1
MAX_TICKS = 24


def multiplier_at(tick: int) -> float:
    """Displayed multiplier after ``tick`` beats."""
    return round(1.0 * (1.14**tick), 2)


def crash_point_from_roll(roll: float, rtp: float = DEFAULT_RTP) -> float:
    """The multiplier at which a round dies, given a uniform ``roll``.

    ``roll`` is uniform in ``[0, 1)``. Values at or above ``rtp`` are instant
    busts; otherwise the crash point is ``rtp / roll``, which gives exactly
    ``P(crash >= x) = rtp / x``.
    """
    if roll >= rtp:
        return 1.00
    return round(max(1.00, rtp / max(roll, 1e-12)), 2)


@dataclass(slots=True)
class Round:
    """State of one crash round."""

    stake: int
    crash_at: float
    rtp: float = DEFAULT_RTP
    tick: int = 0
    cashed_at: float | None = None
    history: list[float] = field(default_factory=list)

    @classmethod
    def create(cls, stake: int, fairness: Fairness, rtp: float = DEFAULT_RTP) -> "Round":
        return cls(stake=stake, crash_at=crash_point_from_roll(fairness.float(), rtp), rtp=rtp)

    @property
    def current(self) -> float:
        return multiplier_at(self.tick)

    @property
    def crashed(self) -> bool:
        return self.cashed_at is None and self.current >= self.crash_at

    @property
    def finished(self) -> bool:
        return self.cashed_at is not None or self.crashed or self.tick >= MAX_TICKS

    def advance(self) -> bool:
        """Move one beat forward. Returns ``False`` once the round is over."""
        if self.finished:
            return False
        self.tick += 1
        self.history.append(self.current)
        return not self.finished

    def cash_out(self) -> float:
        """Bank the current multiplier. Raises if the round already ended."""
        if self.finished:
            raise RuntimeError("round already finished")
        self.cashed_at = self.current
        return self.cashed_at

    def payout(self) -> int:
        if self.cashed_at is None:
            return 0
        return int(self.stake * self.cashed_at)
