"""Cards, hands, and a multi-deck shoe.

Shared by blackjack (and available to anything else that needs cards). The
shoe holds six decks and reshuffles when three quarters of it is gone, which
is both how casinos do it and what stops a long session from drawing from a
near-empty deck.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from casino.games.rng import Fairness

RANKS: tuple[str, ...] = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
SUITS: tuple[str, ...] = ("♠", "♥", "♦", "♣")
RED_SUITS = frozenset({"♥", "♦"})

DECKS_IN_SHOE = 6
RESHUFFLE_AT = 0.25  # reshuffle once only a quarter of the shoe is left


@dataclass(frozen=True, slots=True)
class Card:
    rank: str
    suit: str

    @property
    def value(self) -> int:
        """Blackjack value, aces counted high; softening happens in ``total``."""
        if self.rank == "A":
            return 11
        if self.rank in ("J", "Q", "K"):
            return 10
        return int(self.rank)

    @property
    def is_ace(self) -> bool:
        return self.rank == "A"

    def __str__(self) -> str:  # ``A♠``
        return f"{self.rank}{self.suit}"

    def render(self) -> str:
        """Monospaced so hands line up in an embed regardless of rank width."""
        return f"`{self.rank}{self.suit}`"


HIDDEN = "`🂠`"


def full_deck() -> list[Card]:
    return [Card(rank, suit) for suit in SUITS for rank in RANKS]


def total(cards: list[Card]) -> tuple[int, bool]:
    """Best total for a hand, plus whether it is *soft* (an ace counts as 11).

    >>> total([Card("A", "♠"), Card("7", "♦")])
    (18, True)
    >>> total([Card("A", "♠"), Card("7", "♦"), Card("9", "♣")])
    (17, False)
    """
    running = sum(card.value for card in cards)
    aces = sum(1 for card in cards if card.is_ace)
    while running > 21 and aces:
        running -= 10
        aces -= 1
    return running, aces > 0


def is_blackjack(cards: list[Card]) -> bool:
    """Exactly two cards totalling 21. A 21 built after a split is not one."""
    return len(cards) == 2 and total(cards)[0] == 21


def is_bust(cards: list[Card]) -> bool:
    return total(cards)[0] > 21


def render_hand(cards: list[Card], hide_from: int | None = None) -> str:
    parts = []
    for index, card in enumerate(cards):
        parts.append(HIDDEN if hide_from is not None and index >= hide_from else card.render())
    return " ".join(parts)


def describe_total(cards: list[Card]) -> str:
    value, soft = total(cards)
    if is_blackjack(cards):
        return "**Blackjack!**"
    if value > 21:
        return f"**{value}** · bust"
    return f"**{value}**{' (soft)' if soft and value < 21 else ''}"


@dataclass(slots=True)
class Shoe:
    """A seeded, self-reshuffling stack of cards."""

    fairness: Fairness
    decks: int = DECKS_IN_SHOE
    cards: list[Card] = field(default_factory=list)
    shuffles: int = 0

    def __post_init__(self) -> None:
        if not self.cards:
            self.refill()

    @property
    def size(self) -> int:
        return self.decks * 52

    def refill(self) -> None:
        pool: list[Card] = []
        for _ in range(self.decks):
            pool.extend(full_deck())
        self.cards = self.fairness.shuffled(pool)
        self.shuffles += 1

    def draw(self) -> Card:
        if len(self.cards) <= self.size * RESHUFFLE_AT:
            self.refill()
        return self.cards.pop()

    def deal(self, count: int) -> list[Card]:
        return [self.draw() for _ in range(count)]
