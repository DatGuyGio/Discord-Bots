"""Game maths.

These tests exist because the old bot's numbers were wrong in ways nobody
could see from reading the code: slots returned ~128% of stake, dice returned
exactly 100%, and "mines" ignored the grid entirely. Each assertion below
pins a number that used to be free to drift.
"""

from __future__ import annotations

from itertools import product

import pytest

from casino.games import crash, mines, roulette, slots
from casino.games.payouts import fair_multiplier, profit_multiplier
from casino.games.rng import Fairness, commitment, verify

# ---------------------------------------------------------------------------
# payouts
# ---------------------------------------------------------------------------


def test_fair_multiplier_is_break_even_at_full_rtp():
    # A 1-in-6 shot must return 6x to be a fair bet.
    assert fair_multiplier(1 / 6, rtp=1.0) == pytest.approx(6.0)
    assert profit_multiplier(0.5, rtp=1.0) == pytest.approx(1.0)


def test_house_edge_comes_out_of_the_multiplier():
    assert fair_multiplier(0.5, rtp=0.97) == pytest.approx(1.94)


@pytest.mark.parametrize("probability", [0.01, 0.1, 1 / 6, 0.5, 0.97, 1.0])
def test_expected_value_equals_rtp(probability):
    """Whatever the odds, staking one chip returns `rtp` on average."""
    assert probability * fair_multiplier(probability, rtp=0.95) == pytest.approx(0.95)


@pytest.mark.parametrize("bad", [0, -0.5, 1.5])
def test_impossible_probabilities_are_rejected(bad):
    with pytest.raises(ValueError):
        fair_multiplier(bad)


# ---------------------------------------------------------------------------
# slots
# ---------------------------------------------------------------------------


def test_slots_return_to_player_is_in_the_advertised_band():
    rtp = slots.theoretical_rtp()
    # The old flat paytable measured 1.28 here, i.e. the game printed chips.
    assert 0.93 <= rtp <= 0.96, f"slots RTP drifted to {rtp:.4f}"


def test_slots_rtp_matches_enumeration_of_every_outcome():
    """Cross-check the helper against an independent enumeration."""
    total = 0.0
    weight_total = sum(symbol.weight for symbol in slots.REELS)
    for combo in product(slots.REELS, repeat=3):
        chance = 1.0
        for symbol in combo:
            chance *= symbol.weight / weight_total
        first, second, third = combo
        if first is second is third:
            payout = first.triple
        else:
            keys = [s.key for s in combo]
            pair = next((s for s in combo if keys.count(s.key) == 2), None)
            payout = pair.pair if pair else 0.0
        total += chance * payout
    assert total == pytest.approx(slots.theoretical_rtp())


def test_rarer_symbols_never_pay_less():
    """The paytable must be monotone in rarity, or strategy becomes nonsense."""
    ordered = sorted(slots.REELS, key=lambda s: s.weight, reverse=True)
    for earlier, later in zip(ordered, ordered[1:]):
        assert later.triple >= earlier.triple
        assert later.pair >= earlier.pair


def test_slots_hit_rate_is_engaging_but_not_free():
    assert 0.4 <= slots.hit_rate() <= 0.55


def test_classify_identifies_each_shape():
    cherry, lemon = slots.BY_KEY["cherry"], slots.BY_KEY["lemon"]
    orange = slots.BY_KEY["orange"]
    assert slots.classify((cherry, cherry, cherry))[0] == "triple"
    assert slots.classify((cherry, cherry, lemon))[0] == "pair"
    assert slots.classify((cherry, lemon, orange))[0] == "miss"
    # A pair is recognised regardless of which reels hold it.
    assert slots.classify((lemon, cherry, cherry))[1].key == "cherry"


def test_spin_payout_follows_the_paytable():
    spin = slots.spin(1_000, Fairness(server_seed="a" * 64, client_seed="test"))
    assert spin.payout == int(1_000 * spin.multiplier)


# ---------------------------------------------------------------------------
# roulette
# ---------------------------------------------------------------------------


def test_every_bet_has_the_single_zero_house_edge():
    """2.70% is the defining property of a European wheel."""
    for bet in roulette.OUTSIDE_BETS:
        assert bet.house_edge == pytest.approx(1 / 37, abs=1e-9)
    assert roulette.straight_up(17).house_edge == pytest.approx(1 / 37, abs=1e-9)


def test_colours_and_halves_partition_the_wheel():
    assert len(roulette.RED) == len(roulette.BLACK) == 18
    assert roulette.RED & roulette.BLACK == set()
    assert roulette.RED | roulette.BLACK | roulette.GREEN == set(range(37))


def test_dozens_and_columns_are_twelve_pockets_each():
    for bet in roulette.OUTSIDE_BETS:
        if bet.group in ("dozen", "column"):
            assert len(bet.winning) == 12


def test_zero_loses_every_outside_bet():
    for bet in roulette.OUTSIDE_BETS:
        assert not bet.wins(0)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("17", "17"), ("0", "0"), ("36", "36"),
        ("RED", "red"), (" black ", "black"), ("1st12", "dozen1"),
        ("col3", "column3"), ("19-36", "high"), ("1-18", "low"),
    ],
)
def test_bet_parsing_accepts_the_names_people_actually_type(text, expected):
    bet = roulette.parse_bet(text)
    assert bet is not None and bet.key == expected


@pytest.mark.parametrize("text", ["37", "-1", "purple", "", "col4", None])
def test_nonsense_bets_are_rejected(text):
    assert roulette.parse_bet(text) is None


def test_straight_up_pays_35_to_1_only_on_its_number():
    bet = roulette.straight_up(17)
    win = roulette.spin(bet, 100, 17)
    lose = roulette.spin(bet, 100, 18)
    assert win.won and win.payout == 3_600  # 100 stake + 3,500 profit
    assert not lose.won and lose.payout == 0


def test_payouts_sum_correctly_across_the_whole_wheel():
    """Walking all 37 pockets is the whole sample space, so this is exact."""
    for bet in roulette.OUTSIDE_BETS:
        returned = sum(roulette.spin(bet, 100, pocket).payout for pocket in roulette.POCKETS)
        staked = 100 * 37
        assert returned / staked == pytest.approx(36 / 37)


# ---------------------------------------------------------------------------
# mines
# ---------------------------------------------------------------------------


def test_survival_chance_matches_hand_computed_values():
    assert mines.survival_chance(1, 1) == pytest.approx(19 / 20)
    assert mines.survival_chance(1, 2) == pytest.approx((19 / 20) * (18 / 19))
    assert mines.survival_chance(19, 1) == pytest.approx(1 / 20)
    assert mines.survival_chance(5, 0) == 1.0


def test_multiplier_is_the_fair_price_of_the_risk_taken():
    for count in (1, 3, 5, 12, 19):
        for picks in range(1, mines.TILES - count + 1):
            chance = mines.survival_chance(count, picks)
            assert mines.multiplier(count, picks, rtp=1.0) == pytest.approx(1 / chance)


def test_expected_value_of_stopping_anywhere_is_the_rtp():
    """Cashing out at any point is worth the same — there is no optimal stop."""
    for picks in range(1, 10):
        chance = mines.survival_chance(3, picks)
        assert chance * mines.multiplier(3, picks, rtp=0.97) == pytest.approx(0.97)


def test_more_mines_pays_more():
    assert mines.multiplier(5, 1) > mines.multiplier(1, 1)
    assert mines.multiplier(19, 1) > mines.multiplier(5, 1)


def test_board_pays_nothing_after_an_explosion():
    fairness = Fairness(server_seed="b" * 64)
    board = mines.Board.create(3, fairness)
    bomb = next(iter(board.mine_positions))
    assert board.reveal(bomb) is False
    assert board.payout(1_000) == 0
    assert board.finished


def test_board_pays_the_ladder_after_cashing_out():
    fairness = Fairness(server_seed="c" * 64)
    board = mines.Board.create(3, fairness)
    safe = [tile for tile in range(mines.TILES) if tile not in board.mine_positions]
    for tile in safe[:4]:
        assert board.reveal(tile) is True
    expected = int(1_000 * mines.multiplier(3, 4, board.rtp))
    board.cash_out()
    assert board.payout(1_000) == expected


def test_a_tile_cannot_be_clicked_twice():
    """Without this a double click would advance the multiplier for free."""
    fairness = Fairness(server_seed="d" * 64)
    board = mines.Board.create(1, fairness)
    safe = next(tile for tile in range(mines.TILES) if tile not in board.mine_positions)
    board.reveal(safe)
    with pytest.raises(ValueError):
        board.reveal(safe)


def test_clearing_the_board_ends_the_round_and_pays():
    fairness = Fairness(server_seed="e" * 64)
    board = mines.Board.create(19, fairness)  # a single safe tile
    safe = next(tile for tile in range(mines.TILES) if tile not in board.mine_positions)
    assert board.reveal(safe) is True
    assert board.cleared and board.finished
    assert board.payout(100) == int(100 * mines.multiplier(19, 1, board.rtp))


def test_mine_count_is_clamped_to_a_playable_range():
    fairness = Fairness(server_seed="f" * 64)
    assert mines.Board.create(999, fairness).mines == mines.MAX_MINES
    assert mines.Board.create(-5, fairness).mines == mines.MIN_MINES


# ---------------------------------------------------------------------------
# crash
# ---------------------------------------------------------------------------


def test_crash_distribution_gives_the_intended_rtp():
    """P(crash >= x) must be rtp/x; check it over a fine grid of rolls."""
    rtp = 0.97
    samples = 200_000
    for target in (1.5, 2.0, 5.0, 10.0):
        survived = sum(
            1
            for index in range(samples)
            if crash.crash_point_from_roll((index + 0.5) / samples, rtp) >= target
        )
        assert survived / samples == pytest.approx(rtp / target, abs=0.01)


def test_instant_bust_probability_is_the_house_edge():
    rtp = 0.97
    samples = 100_000
    busts = sum(
        1 for index in range(samples) if crash.crash_point_from_roll((index + 0.5) / samples, rtp) == 1.00
    )
    assert busts / samples == pytest.approx(1 - rtp, abs=0.01)


def test_cashing_out_pays_the_multiplier_reached():
    round_ = crash.Round(stake=1_000, crash_at=5.0)
    round_.advance()
    round_.advance()
    multiplier = round_.cash_out()
    assert round_.payout() == int(1_000 * multiplier)


def test_a_crashed_round_pays_nothing():
    round_ = crash.Round(stake=1_000, crash_at=1.00)
    round_.advance()
    assert round_.crashed
    assert round_.payout() == 0
    with pytest.raises(RuntimeError):
        round_.cash_out()


# ---------------------------------------------------------------------------
# fairness
# ---------------------------------------------------------------------------


def test_the_same_seed_always_reproduces_the_same_round():
    """This is what lets a player re-derive their result and check it."""
    first = Fairness(server_seed="a" * 64, client_seed="player", nonce=7)
    second = Fairness(server_seed="a" * 64, client_seed="player", nonce=7)
    assert [first.float() for _ in range(5)] == [second.float() for _ in range(5)]


def test_a_different_nonce_gives_a_different_round():
    first = Fairness(server_seed="a" * 64, client_seed="player", nonce=1)
    second = Fairness(server_seed="a" * 64, client_seed="player", nonce=2)
    assert first.float() != second.float()


def test_commitment_verifies_only_for_the_real_seed():
    fairness = Fairness()
    assert verify(fairness.server_seed, fairness.commitment)
    assert not verify("not the seed", fairness.commitment)
    assert commitment(fairness.server_seed) == fairness.commitment


def test_derived_floats_stay_in_range_and_spread_out():
    fairness = Fairness(server_seed="z" * 64)
    values = [fairness.float() for _ in range(5_000)]
    assert all(0.0 <= value < 1.0 for value in values)
    assert sum(values) / len(values) == pytest.approx(0.5, abs=0.02)
    # Rough uniformity check: each tenth of the range gets roughly a tenth.
    for bucket in range(10):
        share = sum(1 for value in values if bucket / 10 <= value < (bucket + 1) / 10) / len(values)
        assert share == pytest.approx(0.1, abs=0.02)


def test_seeded_shuffle_is_a_permutation():
    fairness = Fairness(server_seed="y" * 64)
    shuffled = fairness.shuffled(range(52))
    assert sorted(shuffled) == list(range(52))
    assert shuffled != list(range(52))


def test_weighted_choice_respects_the_weights():
    fairness = Fairness(server_seed="x" * 64)
    options = ["common", "rare"]
    draws = [fairness.weighted(options, [99, 1]) for _ in range(2_000)]
    assert draws.count("rare") < 60
