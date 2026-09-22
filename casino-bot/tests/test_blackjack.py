"""Blackjack rules.

Rules are the easiest thing in a casino to get subtly wrong, and the hardest
to notice: a natural that pays 1:1 instead of 3:2, or a dealer that hits soft
17 when the help text says it stands, costs players money quietly. Every rule
in the help text is asserted here.
"""

from __future__ import annotations

import pytest

from casino.games import blackjack as bj
from casino.games.cards import Card, Shoe, full_deck, is_blackjack, render_hand, total
from casino.games.rng import Fairness


def hand(*codes: str) -> list[Card]:
    """``hand("A♠", "10♦")`` -> a list of cards."""
    cards = []
    for code in codes:
        rank, suit = code[:-1], code[-1]
        cards.append(Card(rank, suit))
    return cards


# ---------------------------------------------------------------------------
# totals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cards,expected,soft",
    [
        (("A♠", "7♦"), 18, True),
        (("A♠", "7♦", "9♣"), 17, False),
        (("A♠", "A♦"), 12, True),
        (("A♠", "A♦", "A♣"), 13, True),
        (("A♠", "A♦", "9♣"), 21, True),
        (("K♠", "Q♦"), 20, False),
        (("K♠", "Q♦", "2♣"), 22, False),
        (("2♠", "3♦", "4♣"), 9, False),
        (("A♠", "K♦"), 21, True),
    ],
)
def test_hand_totals_soften_aces_only_when_needed(cards, expected, soft):
    value, is_soft = total(hand(*cards))
    assert value == expected
    assert is_soft is soft


def test_face_cards_are_all_ten():
    for rank in ("10", "J", "Q", "K"):
        assert Card(rank, "♠").value == 10


def test_blackjack_needs_exactly_two_cards():
    assert is_blackjack(hand("A♠", "K♦"))
    assert not is_blackjack(hand("7♠", "7♦", "7♣"))  # 21, but not a natural


def test_a_split_hand_of_21_is_not_a_natural():
    """Otherwise splitting aces would pay 3:2 twice, which no casino allows."""
    split = bj.Hand(cards=hand("A♠", "K♦"), bet=100, from_split=True)
    assert split.total == 21
    assert not split.natural


def test_hidden_cards_render_as_a_back():
    rendered = render_hand(hand("A♠", "K♦"), hide_from=1)
    assert "A♠" in rendered
    assert "K♦" not in rendered


# ---------------------------------------------------------------------------
# dealer policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cards,should_hit",
    [
        (("2♠", "3♦"), True),      # 5
        (("10♠", "6♦"), True),     # 16
        (("10♠", "7♦"), False),    # hard 17
        (("A♠", "6♦"), False),     # soft 17: this house stands
        (("A♠", "6♦", "A♣"), False),  # soft 18
        (("10♠", "10♦"), False),   # 20
    ],
)
def test_dealer_stands_on_all_seventeens(cards, should_hit):
    assert bj.dealer_should_hit(hand(*cards)) is should_hit


def test_dealer_draws_until_policy_is_satisfied():
    shoe = Shoe(Fairness(server_seed="a" * 64))
    cards = hand("2♠", "3♦")
    bj.play_dealer(cards, shoe)
    value, soft = total(cards)
    assert value >= 17
    assert not (value == 17 and soft and bj.DEALER_HITS_SOFT_17)


# ---------------------------------------------------------------------------
# settlement
# ---------------------------------------------------------------------------


def test_natural_pays_three_to_two():
    player = bj.Hand(cards=hand("A♠", "K♦"), bet=100)
    outcome, payout = bj.resolve(player, hand("10♠", "9♦"))
    assert outcome is bj.Outcome.BLACKJACK
    assert payout == 250  # 100 back + 150 profit


def test_two_naturals_push():
    player = bj.Hand(cards=hand("A♠", "K♦"), bet=100)
    outcome, payout = bj.resolve(player, hand("A♣", "Q♦"))
    assert outcome is bj.Outcome.PUSH
    assert payout == 100


def test_a_natural_beats_a_three_card_twenty_one():
    player = bj.Hand(cards=hand("A♠", "K♦"), bet=100)
    outcome, payout = bj.resolve(player, hand("7♠", "7♦", "7♣"))
    assert outcome is bj.Outcome.BLACKJACK
    assert payout == 250


def test_bust_loses_even_when_the_dealer_also_busts():
    """Order matters: the player acts first, so their bust is already final."""
    player = bj.Hand(cards=hand("10♠", "8♦", "9♣"), bet=100)
    outcome, payout = bj.resolve(player, hand("10♥", "8♥", "9♥"))
    assert outcome is bj.Outcome.BUST
    assert payout == 0


def test_dealer_bust_pays_even_money():
    player = bj.Hand(cards=hand("10♠", "8♦"), bet=100)
    outcome, payout = bj.resolve(player, hand("10♥", "8♥", "9♥"))
    assert outcome is bj.Outcome.WIN
    assert payout == 200


def test_equal_totals_return_the_stake():
    player = bj.Hand(cards=hand("10♠", "9♦"), bet=100)
    outcome, payout = bj.resolve(player, hand("10♥", "9♥"))
    assert outcome is bj.Outcome.PUSH
    assert payout == 100


def test_dealer_natural_beats_a_plain_twenty_one():
    player = bj.Hand(cards=hand("7♠", "7♦", "7♣"), bet=100)
    outcome, payout = bj.resolve(player, hand("A♥", "K♥"))
    assert outcome is bj.Outcome.LOSE
    assert payout == 0


def test_a_doubled_hand_settles_on_the_doubled_stake():
    player = bj.Hand(cards=hand("5♠", "6♦", "10♣"), bet=200, doubled=True)
    outcome, payout = bj.resolve(player, hand("10♥", "7♥"))
    assert outcome is bj.Outcome.WIN
    assert payout == 400


def test_insurance_pays_two_to_one_only_against_a_natural():
    assert bj.insurance_result(hand("A♠", "K♦"), 50) == (True, 150)
    assert bj.insurance_result(hand("A♠", "9♦"), 50) == (False, 0)
    assert bj.insurance_result(hand("A♠", "K♦"), 0) == (False, 0)


# ---------------------------------------------------------------------------
# seat actions
# ---------------------------------------------------------------------------


def make_seat(cards: list[Card], bet: int = 100) -> tuple[bj.Seat, Shoe]:
    shoe = Shoe(Fairness(server_seed="b" * 64))
    seat = bj.Seat(user_id=1, hands=[bj.Hand(cards=cards, bet=bet)])
    return seat, shoe


def test_double_takes_exactly_one_card_and_stands():
    seat, shoe = make_seat(hand("5♠", "6♦"))
    assert seat.double(shoe) is True
    assert seat.current.bet == 200
    assert len(seat.current.cards) == 3
    assert seat.current.finished


def test_double_is_refused_after_a_third_card():
    seat, shoe = make_seat(hand("5♠", "6♦"))
    seat.hit(shoe)
    assert seat.current.can_double is False
    assert seat.double(shoe) is False


def test_split_creates_a_second_hand_with_the_same_bet():
    seat, shoe = make_seat(hand("8♠", "8♦"))
    assert seat.split(shoe) is True
    assert len(seat.hands) == 2
    assert [h.bet for h in seat.hands] == [100, 100]
    assert all(len(h.cards) == 2 for h in seat.hands)
    assert seat.total_staked == 200


def test_split_matches_on_value_so_king_queen_can_split():
    seat, shoe = make_seat(hand("K♠", "Q♦"))
    assert seat.current.can_split(1) is True


def test_split_aces_take_one_card_each_and_stand():
    seat, shoe = make_seat(hand("A♠", "A♦"))
    seat.split(shoe)
    assert all(h.stood for h in seat.hands)
    assert seat.done


def test_a_hand_can_only_be_split_once():
    seat, shoe = make_seat(hand("8♠", "8♦"))
    seat.split(shoe)
    assert seat.current.can_split(len(seat.hands)) is False
    assert seat.split(shoe) is False


def test_advance_walks_to_the_next_unfinished_hand():
    seat, shoe = make_seat(hand("8♠", "8♦"))
    seat.split(shoe)
    seat.hands[0].stood = True
    assert seat.advance() is True
    assert seat.active == 1
    seat.hands[1].stood = True
    assert seat.advance() is False
    assert seat.done


# ---------------------------------------------------------------------------
# shoe
# ---------------------------------------------------------------------------


def test_shoe_holds_the_configured_number_of_decks():
    shoe = Shoe(Fairness(server_seed="c" * 64))
    assert len(shoe.cards) == 52 * shoe.decks
    assert len(full_deck()) == 52


def test_shoe_reshuffles_before_running_out():
    shoe = Shoe(Fairness(server_seed="d" * 64), decks=1)
    for _ in range(200):  # far more cards than a single deck holds
        assert shoe.draw() is not None
    assert shoe.shuffles > 1


def test_shoe_is_reproducible_from_its_seed():
    first = Shoe(Fairness(server_seed="e" * 64))
    second = Shoe(Fairness(server_seed="e" * 64))
    assert [str(first.draw()) for _ in range(20)] == [str(second.draw()) for _ in range(20)]


def test_no_card_repeats_before_the_shoe_reshuffles():
    """Cards are dealt without replacement, so a single-deck shoe cannot show
    the same card twice before it reaches its reshuffle point."""
    shoe = Shoe(Fairness(server_seed="f" * 64), decks=1)
    dealable = int(52 * (1 - 0.25))
    drawn = [str(shoe.draw()) for _ in range(dealable)]
    assert shoe.shuffles == 1
    assert len(set(drawn)) == dealable
