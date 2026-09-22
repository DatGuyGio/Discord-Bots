"""Payout maths.

A casino game is fully described by two numbers: the chance of winning and
what a win returns. Rather than hardcoding multipliers per game (the old bot
paid 5:1 on a 1-in-6 dice roll, which is a 100% return-to-player and so a
money printer), every game derives its payout from its own probability and a
single return-to-player constant.
"""

from __future__ import annotations

from casino.config import DEFAULT_RTP


def fair_multiplier(probability: float, rtp: float = DEFAULT_RTP) -> float:
    """Total return per chip staked for a bet that wins ``probability`` of the time.

    With ``rtp=1`` this is the break-even multiplier: a 1-in-6 bet returns 6x,
    so the expected value is exactly the stake. Scaling by ``rtp`` shaves the
    house's cut off the top, which is where the edge comes from.

    >>> round(fair_multiplier(1 / 6, rtp=1.0), 4)
    6.0
    >>> round(fair_multiplier(0.5, rtp=0.97), 4)
    1.94
    """
    if not 0 < probability <= 1:
        raise ValueError(f"probability must be in (0, 1], got {probability!r}")
    return rtp / probability


def profit_multiplier(probability: float, rtp: float = DEFAULT_RTP) -> float:
    """Profit (excluding the returned stake) per chip staked.

    >>> round(profit_multiplier(0.5, rtp=0.97), 4)
    0.94
    """
    return fair_multiplier(probability, rtp) - 1.0


def payout(stake: int, probability: float, rtp: float = DEFAULT_RTP) -> int:
    """Total chips handed back on a win, rounded down to a whole chip."""
    return int(stake * fair_multiplier(probability, rtp))
