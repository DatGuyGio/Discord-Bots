"""Blackjack rules, with no Discord anywhere near them.

The old implementation mixed dealing, betting, message editing and payout
maths into one 200-line coroutine, so the only way to check the rules was to
play a hand in Discord. Here the rules are pure functions over plain data:
``tests/test_blackjack.py`` plays thousands of hands in milliseconds.

House rules implemented:

* Six-deck shoe, dealer stands on all 17s (including soft 17).
* Naturals pay 3:2 and beat a dealer 21 made from three or more cards.
* Double down on any two cards; one card is then dealt and the hand stands.
* Split any pair once, giving at most two hands. Split aces get one card each.
* Insurance offered when the dealer shows an ace; pays 2:1.
* Player and dealer naturals push.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from casino.games.cards import Card, Shoe, is_blackjack, total

DEALER_STANDS_ON = 17
DEALER_HITS_SOFT_17 = False
BLACKJACK_PAYOUT = 1.5
INSURANCE_PAYOUT = 2.0
MAX_HANDS = 2


class Outcome(str, Enum):
    BLACKJACK = "blackjack"
    WIN = "win"
    PUSH = "push"
    LOSE = "lose"
    BUST = "bust"

    @property
    def label(self) -> str:
        return {
            Outcome.BLACKJACK: "Blackjack",
            Outcome.WIN: "Win",
            Outcome.PUSH: "Push",
            Outcome.LOSE: "Lose",
            Outcome.BUST: "Bust",
        }[self]


@dataclass(slots=True)
class Hand:
    """One betting box. A split turns one hand into two."""

    cards: list[Card] = field(default_factory=list)
    bet: int = 0
    doubled: bool = False
    stood: bool = False
    from_split: bool = False

    @property
    def total(self) -> int:
        return total(self.cards)[0]

    @property
    def soft(self) -> bool:
        return total(self.cards)[1]

    @property
    def busted(self) -> bool:
        return self.total > 21

    @property
    def natural(self) -> bool:
        # A 21 made from split cards is a plain 21, never a natural.
        return not self.from_split and is_blackjack(self.cards)

    @property
    def finished(self) -> bool:
        return self.stood or self.busted or self.natural or self.total == 21

    @property
    def can_double(self) -> bool:
        return len(self.cards) == 2 and not self.doubled and not self.from_split

    def can_split(self, hand_count: int) -> bool:
        return (
            len(self.cards) == 2
            and hand_count < MAX_HANDS
            and not self.from_split
            and self.cards[0].value == self.cards[1].value
        )


def dealer_should_hit(cards: list[Card]) -> bool:
    """Dealer policy. One place, so 'stands on 17' can never drift."""
    value, soft = total(cards)
    if value < DEALER_STANDS_ON:
        return True
    return DEALER_HITS_SOFT_17 and value == 17 and soft


def play_dealer(cards: list[Card], shoe: Shoe) -> list[Card]:
    """Draw for the dealer until policy says stop. Returns the cards drawn."""
    drawn: list[Card] = []
    while dealer_should_hit(cards):
        card = shoe.draw()
        cards.append(card)
        drawn.append(card)
    return drawn


def resolve(hand: Hand, dealer: list[Card]) -> tuple[Outcome, int]:
    """Score one hand against the dealer.

    Returns the outcome and the **total chips returned** to the player
    (stake included), so a push returns the bet and a loss returns zero.
    """
    dealer_total = total(dealer)[0]
    dealer_natural = is_blackjack(dealer)

    if hand.natural:
        if dealer_natural:
            return Outcome.PUSH, hand.bet
        return Outcome.BLACKJACK, int(hand.bet * (1 + BLACKJACK_PAYOUT))
    if hand.busted:
        return Outcome.BUST, 0
    if dealer_natural:
        return Outcome.LOSE, 0
    if dealer_total > 21:
        return Outcome.WIN, hand.bet * 2
    if hand.total > dealer_total:
        return Outcome.WIN, hand.bet * 2
    if hand.total == dealer_total:
        return Outcome.PUSH, hand.bet
    return Outcome.LOSE, 0


def insurance_result(dealer: list[Card], insurance_bet: int) -> tuple[bool, int]:
    """Insurance pays 2:1 when the dealer's hole card completes a natural."""
    if insurance_bet <= 0:
        return False, 0
    if is_blackjack(dealer):
        return True, int(insurance_bet * (1 + INSURANCE_PAYOUT))
    return False, 0


@dataclass(slots=True)
class Seat:
    """A player's whole position at the table: their hands and side bets."""

    user_id: int
    hands: list[Hand] = field(default_factory=list)
    insurance: int = 0
    active: int = 0  # index of the hand currently being played

    @property
    def total_staked(self) -> int:
        return sum(hand.bet for hand in self.hands) + self.insurance

    @property
    def current(self) -> Hand:
        return self.hands[self.active]

    @property
    def done(self) -> bool:
        return all(hand.finished for hand in self.hands)

    def advance(self) -> bool:
        """Move to the next unfinished hand. ``False`` when the seat is done."""
        for index in range(self.active, len(self.hands)):
            if not self.hands[index].finished:
                self.active = index
                return True
        return False

    def split(self, shoe: Shoe) -> bool:
        hand = self.current
        if not hand.can_split(len(self.hands)):
            return False
        moved = hand.cards.pop()
        hand.from_split = True
        new_hand = Hand(cards=[moved], bet=hand.bet, from_split=True)
        hand.cards.append(shoe.draw())
        new_hand.cards.append(shoe.draw())
        # Split aces receive a single card and stand, as at a real table.
        if moved.is_ace:
            hand.stood = True
            new_hand.stood = True
        self.hands.append(new_hand)
        return True

    def double(self, shoe: Shoe) -> bool:
        hand = self.current
        if not hand.can_double:
            return False
        hand.bet *= 2
        hand.doubled = True
        hand.cards.append(shoe.draw())
        hand.stood = True
        return True

    def hit(self, shoe: Shoe) -> Card:
        card = shoe.draw()
        self.current.cards.append(card)
        return card
