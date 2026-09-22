"""Three-reel slots with a paytable that actually holds an edge.

The original paytable paid a flat 3x profit for *any* three-of-a-kind and 1x
profit for any two matches. Because cherries landed 30% of the time, the
expected return worked out to roughly **128% of stake** — every spin was a
positive-expectation bet and slots quietly printed chips into the economy.

The table below is tiered by symbol rarity and has a measured return of
~94.8%, which ``tests/test_slots.py`` asserts exactly (by enumerating all
512 reel combinations, not by simulation).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from casino.games.rng import Fairness


@dataclass(frozen=True, slots=True)
class Symbol:
    key: str
    emoji: str
    name: str
    weight: int
    triple: float  # total return per chip staked for three of a kind
    pair: float  # total return per chip staked for exactly two


#: Ordered rarest-last so the paytable reads like a ladder.
REELS: tuple[Symbol, ...] = (
    Symbol("cherry", "🍒", "Cherry", 30, 4, 1.0),
    Symbol("lemon", "🍋", "Lemon", 22, 8, 1.0),
    Symbol("orange", "🍊", "Orange", 16, 14, 1.2),
    Symbol("melon", "🍉", "Melon", 12, 25, 1.5),
    Symbol("bell", "🔔", "Bell", 9, 45, 2.5),
    Symbol("star", "⭐", "Star", 6, 90, 4.0),
    Symbol("diamond", "💎", "Diamond", 4, 200, 10.0),
    Symbol("seven", "7️⃣", "Lucky Seven", 1, 1500, 30.0),
)

BY_KEY = {symbol.key: symbol for symbol in REELS}
BY_EMOJI = {symbol.emoji: symbol for symbol in REELS}
WEIGHTS = tuple(symbol.weight for symbol in REELS)
TOTAL_WEIGHT = sum(WEIGHTS)


def probability(symbol: Symbol) -> float:
    return symbol.weight / TOTAL_WEIGHT


@dataclass(slots=True)
class SpinResult:
    reels: tuple[Symbol, Symbol, Symbol]
    kind: str  # "triple" | "pair" | "miss"
    symbol: Symbol | None
    multiplier: float  # total return per chip staked
    payout: int  # total chips returned, stake included

    @property
    def display(self) -> str:
        return "  ".join(symbol.emoji for symbol in self.reels)

    @property
    def is_jackpot(self) -> bool:
        return self.kind == "triple" and self.symbol is not None and self.symbol.key == "seven"

    @property
    def headline(self) -> str:
        if self.is_jackpot:
            return "JACKPOT — TRIPLE SEVENS"
        if self.kind == "triple":
            return f"Three {self.symbol.name}s"
        if self.kind == "pair":
            return f"Two {self.symbol.name}s"
        return "No match"


def classify(reels: tuple[Symbol, Symbol, Symbol]) -> tuple[str, Symbol | None, float]:
    """Work out what a set of three reels is worth."""
    first, second, third = reels
    if first is second is third:
        return "triple", first, first.triple
    for candidate in (first, second, third):
        if [symbol.key for symbol in reels].count(candidate.key) == 2:
            return "pair", candidate, candidate.pair
    return "miss", None, 0.0


def spin(stake: int, fairness: Fairness) -> SpinResult:
    """Draw three reels from the seeded stream and price the result."""
    reels = tuple(fairness.weighted(REELS, WEIGHTS) for _ in range(3))
    kind, symbol, multiplier = classify(reels)  # type: ignore[arg-type]
    return SpinResult(
        reels=reels,  # type: ignore[arg-type]
        kind=kind,
        symbol=symbol,
        multiplier=multiplier,
        payout=int(stake * multiplier),
    )


def theoretical_rtp() -> float:
    """Exact return-to-player, by enumerating every reel combination.

    8 symbols on 3 reels is only 512 outcomes, so there is no reason to
    estimate this with a simulation.
    """
    total = 0.0
    for combo in product(REELS, repeat=3):
        chance = 1.0
        for symbol in combo:
            chance *= probability(symbol)
        _, _, multiplier = classify(combo)  # type: ignore[arg-type]
        total += chance * multiplier
    return total


def hit_rate() -> float:
    """Share of spins that return anything at all."""
    total = 0.0
    for combo in product(REELS, repeat=3):
        _, _, multiplier = classify(combo)  # type: ignore[arg-type]
        if multiplier > 0:
            chance = 1.0
            for symbol in combo:
                chance *= probability(symbol)
            total += chance
    return total


def paytable_lines() -> list[str]:
    """Rows for the in-Discord paytable, rarest first."""
    lines = []
    for symbol in reversed(REELS):
        odds = probability(symbol) ** 3
        lines.append(
            f"{symbol.emoji}{symbol.emoji}{symbol.emoji} **x{symbol.triple:g}** "
            f"· pair **x{symbol.pair:g}** · 1 in {int(1 / odds):,}"
        )
    return lines
