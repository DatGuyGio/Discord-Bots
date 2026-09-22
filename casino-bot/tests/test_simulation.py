"""End-to-end economy simulation.

The unit tests prove each piece behaves; this one plays thousands of real
rounds through the real stack — seeded RNG, real paytables, real atomic
wagers, real ledger — and checks the properties that only show up in
aggregate:

* chips are conserved (every chip a player holds was minted by a faucet or
  won from the house, never from nowhere),
* the ledger reconstructs every balance exactly,
* the realised return to player converges on the configured one.

The last point is the one that matters most. The old slots paytable returned
128% of stake, which no amount of unit testing of individual spins would have
revealed, but ten thousand spins makes obvious immediately.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from casino.config import GUILD_DEFAULTS
from casino.db.database import Database
from casino.db.economy import Economy
from casino.games import crash, mines, roulette, slots
from casino.games.payouts import fair_multiplier
from casino.games.rng import Fairness

GUILD = 77
PLAYERS = [101, 102, 103]
ROUNDS = 4_000
RTP = 0.97


@pytest_asyncio.fixture
async def economy(tmp_path):
    db = await Database(str(tmp_path / "sim.sqlite3")).connect()
    repo = Economy(db)
    for player in PLAYERS:
        await repo.ensure(GUILD, player, 1_000_000)
    yield repo
    await db.close()


async def ledger_balance(economy: Economy, user_id: int) -> int:
    """Rebuild a balance from the ledger alone."""
    rows = await economy.db.fetchall(
        "SELECT kind, delta FROM ledger WHERE guild_id = ? AND user_id = ? ORDER BY id",
        (GUILD, user_id),
    )
    return 1_000_000 + sum(int(row["delta"]) for row in rows)


# ---------------------------------------------------------------------------


async def test_slots_return_matches_its_published_paytable(economy):
    player = PLAYERS[0]
    fairness = Fairness(server_seed="s" * 64, client_seed="sim")
    staked = returned = 0
    for nonce in range(ROUNDS):
        fairness.nonce = nonce
        wager = await economy.place_wager(GUILD, player, 100, "slots")
        spin = slots.spin(wager.stake, fairness)
        settlement = await economy.settle(wager, spin.payout)
        staked += settlement.stake
        returned += settlement.payout

    realised = returned / staked
    assert realised == pytest.approx(slots.theoretical_rtp(), abs=0.05)
    # And, crucially, the house is not losing money.
    assert realised < 1.0


async def test_roulette_return_matches_the_single_zero_wheel(economy):
    player = PLAYERS[1]
    fairness = Fairness(server_seed="r" * 64, client_seed="sim")
    bet = roulette.parse_bet("red")
    staked = returned = 0
    for nonce in range(ROUNDS):
        fairness.nonce = nonce
        wager = await economy.place_wager(GUILD, player, 100, "roulette")
        pocket = fairness.below(37)
        outcome = roulette.spin(bet, wager.stake, pocket)
        settlement = await economy.settle(wager, outcome.payout)
        staked += settlement.stake
        returned += settlement.payout
    assert returned / staked == pytest.approx(36 / 37, abs=0.05)


async def test_mines_return_matches_the_configured_rtp(economy):
    """A fixed strategy — always bank after three tiles — must return the RTP."""
    player = PLAYERS[2]
    staked = returned = 0
    for nonce in range(2_000):
        fairness = Fairness(server_seed="m" * 64, client_seed="sim", nonce=nonce)
        wager = await economy.place_wager(GUILD, player, 100, "mines")
        board = mines.Board.create(3, fairness, RTP)
        for tile in range(mines.TILES):
            if board.finished or board.picks >= 3:
                break
            board.reveal(tile)
        if not board.finished:
            board.cash_out()
        settlement = await economy.settle(wager, board.payout(wager.stake))
        staked += settlement.stake
        returned += settlement.payout
    assert returned / staked == pytest.approx(RTP, abs=0.08)


def test_crash_return_matches_the_configured_rtp_for_any_target():
    """Cashing out at 2x or at 10x must have the same expected value."""
    samples = 100_000
    for target in (1.5, 2.0, 4.0, 10.0):
        returned = sum(
            target
            for index in range(samples)
            if crash.crash_point_from_roll((index + 0.5) / samples, RTP) >= target
        )
        assert returned / samples == pytest.approx(RTP, abs=0.02)


def test_coinflip_and_dice_carry_the_same_edge():
    """Every fixed-odds game is priced from one formula, so this is a check
    that no game was given a hand-written multiplier by mistake."""
    for probability in (0.5, 1 / 6, 2 / 6, 5 / 6):
        assert probability * fair_multiplier(probability, RTP) == pytest.approx(RTP)


# ---------------------------------------------------------------------------


async def test_the_ledger_reconstructs_every_balance(economy):
    fairness = Fairness(server_seed="l" * 64)
    settings = dict(GUILD_DEFAULTS)
    for index, player in enumerate(PLAYERS):
        for nonce in range(200):
            fairness.nonce = nonce + index * 1_000
            wager = await economy.place_wager(GUILD, player, 250, "slots")
            spin = slots.spin(wager.stake, fairness)
            await economy.settle(wager, spin.payout)
        await economy.claim_daily(GUILD, player, settings)
        await economy.work(GUILD, player, settings)

    for player in PLAYERS:
        assert await economy.balance(GUILD, player) == await ledger_balance(economy, player)


async def test_transfers_never_change_the_total_chip_supply(economy):
    total_before = sum([await economy.balance(GUILD, player) for player in PLAYERS])
    for _ in range(50):
        await economy.transfer(GUILD, PLAYERS[0], PLAYERS[1], 1_000)
        await economy.transfer(GUILD, PLAYERS[1], PLAYERS[2], 500)
    total_after = sum([await economy.balance(GUILD, player) for player in PLAYERS])
    assert total_after == total_before


async def test_no_balance_ever_dips_below_zero_during_a_session(economy):
    """Play until nearly broke with a fixed stake, checking after every round."""
    player = PLAYERS[0]
    await economy.set_balance(GUILD, player, 5_000)
    fairness = Fairness(server_seed="n" * 64)
    for nonce in range(400):
        fairness.nonce = nonce
        balance = await economy.balance(GUILD, player)
        if balance < 100:
            break
        wager = await economy.place_wager(GUILD, player, 100, "slots")
        spin = slots.spin(wager.stake, fairness)
        await economy.settle(wager, spin.payout)
        assert await economy.balance(GUILD, player) >= 0


async def test_stats_agree_with_the_ledger_after_a_long_session(economy):
    player = PLAYERS[1]
    fairness = Fairness(server_seed="o" * 64)
    for nonce in range(300):
        fairness.nonce = nonce
        wager = await economy.place_wager(GUILD, player, 100, "slots")
        spin = slots.spin(wager.stake, fairness)
        await economy.settle(wager, spin.payout)

    profile = await economy.profile(GUILD, player)
    staked = await economy.db.fetchval(
        "SELECT SUM(stake) FROM ledger WHERE guild_id = ? AND user_id = ? AND kind = 'game'",
        (GUILD, player),
    )
    net = await economy.db.fetchval(
        "SELECT SUM(delta) FROM ledger WHERE guild_id = ? AND user_id = ? AND kind = 'game'",
        (GUILD, player),
    )
    assert profile["games"] == 300
    assert profile["wagered"] == int(staked)
    assert profile["net"] == int(net)
    assert profile["wins"] + profile["losses"] + profile["pushes"] == 300
