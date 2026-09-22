"""The chip ledger.

These are the tests the old bot could not have had, because it had nothing to
test: balances lived in a dict and a "transaction" was a read, a mutation and
a whole-file rewrite. The invariants asserted here are the ones the saved
JSON data shows were violated in production — a balance that went negative,
a stake that exceeded the configured maximum, and a wager counted twice.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio

from casino.config import GUILD_DEFAULTS, MAX_CHIPS
from casino.core.errors import InsufficientFunds, OnCooldown
from casino.db.database import Database
from casino.db.economy import Economy, describe_upgrade, merge_wagers, utcnow
from casino.db.guilds import GuildStore, UnknownSetting
from casino.db.progression import Progression

GUILD = 1234
ALICE = 1
BOB = 2


@pytest_asyncio.fixture
async def db(tmp_path):
    database = await Database(str(tmp_path / "test.sqlite3")).connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def economy(db):
    repo = Economy(db)
    await repo.ensure(GUILD, ALICE, 1_000)
    await repo.ensure(GUILD, BOB, 1_000)
    return repo


@pytest.fixture
def settings():
    return dict(GUILD_DEFAULTS)


# ---------------------------------------------------------------------------
# balances
# ---------------------------------------------------------------------------


async def test_a_new_player_starts_on_the_configured_balance(economy):
    assert await economy.balance(GUILD, ALICE) == 1_000


async def test_ensure_is_idempotent(economy):
    await economy.adjust(GUILD, ALICE, 500, kind="admin")
    await economy.ensure(GUILD, ALICE, 1_000)
    assert await economy.balance(GUILD, ALICE) == 1_500


async def test_balances_are_scoped_per_guild(economy):
    await economy.ensure(9999, ALICE, 250)
    await economy.adjust(GUILD, ALICE, 5_000, kind="admin")
    assert await economy.balance(9999, ALICE) == 250


async def test_spending_more_than_you_have_is_refused(economy):
    with pytest.raises(InsufficientFunds):
        await economy.adjust(GUILD, ALICE, -1_001, kind="shop")
    assert await economy.balance(GUILD, ALICE) == 1_000


async def test_admins_can_push_a_balance_down_but_never_below_zero(economy):
    balance = await economy.adjust(GUILD, ALICE, -5_000, kind="admin", allow_negative=True)
    assert balance == 0


async def test_balances_are_capped_so_an_exploit_cannot_overflow_them(economy):
    balance = await economy.adjust(GUILD, ALICE, MAX_CHIPS * 10, kind="admin")
    assert balance == MAX_CHIPS


# ---------------------------------------------------------------------------
# wagers: the core invariant
# ---------------------------------------------------------------------------


async def test_placing_a_wager_debits_immediately(economy):
    wager = await economy.place_wager(GUILD, ALICE, 400, "slots")
    assert wager.stake == 400
    assert await economy.balance(GUILD, ALICE) == 600


async def test_a_wager_you_cannot_afford_moves_nothing(economy):
    with pytest.raises(InsufficientFunds) as error:
        await economy.place_wager(GUILD, ALICE, 1_001, "slots")
    assert error.value.balance == 1_000
    assert await economy.balance(GUILD, ALICE) == 1_000


async def test_concurrent_bets_cannot_spend_the_same_chips_twice(economy):
    """The bug that produced the 10^27 wager in the saved data.

    Ten simultaneous 200-chip bets against a 1,000 chip balance: exactly five
    must succeed. A read-modify-write implementation lets all ten through,
    because each one reads 1,000 before any of them writes.
    """
    results = await asyncio.gather(
        *(economy.place_wager(GUILD, ALICE, 200, "slots") for _ in range(10)),
        return_exceptions=True,
    )
    accepted = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, InsufficientFunds)]
    assert len(accepted) == 5
    assert len(refused) == 5
    assert await economy.balance(GUILD, ALICE) == 0


async def test_a_balance_can_never_go_negative_under_load(economy):
    await economy.set_balance(GUILD, ALICE, 1_000)
    await asyncio.gather(
        *(economy.place_wager(GUILD, ALICE, 3, "dice") for _ in range(500)),
        return_exceptions=True,
    )
    assert await economy.balance(GUILD, ALICE) >= 0


async def test_settling_credits_the_payout_and_records_the_net(economy):
    wager = await economy.place_wager(GUILD, ALICE, 400, "roulette")
    settlement = await economy.settle(wager, 800)
    assert settlement.net == 400
    assert settlement.balance == 1_400
    assert await economy.balance(GUILD, ALICE) == 1_400


async def test_a_push_returns_the_stake_exactly(economy):
    wager = await economy.place_wager(GUILD, ALICE, 400, "blackjack")
    settlement = await economy.settle(wager, 400)
    assert settlement.net == 0
    assert settlement.pushed
    assert await economy.balance(GUILD, ALICE) == 1_000


async def test_a_loss_leaves_the_stake_with_the_house(economy):
    wager = await economy.place_wager(GUILD, ALICE, 400, "slots")
    settlement = await economy.settle(wager, 0)
    assert settlement.net == -400
    assert await economy.balance(GUILD, ALICE) == 600


async def test_a_wager_cannot_be_settled_twice(economy):
    """Otherwise a retried interaction would pay a winning round out twice."""
    wager = await economy.place_wager(GUILD, ALICE, 100, "slots")
    await economy.settle(wager, 500)
    with pytest.raises(RuntimeError):
        await economy.settle(wager, 500)
    assert await economy.balance(GUILD, ALICE) == 1_400


async def test_refunding_returns_the_stake_without_recording_a_game(economy):
    wager = await economy.place_wager(GUILD, ALICE, 250, "mines")
    await economy.refund(wager)
    assert await economy.balance(GUILD, ALICE) == 1_000
    profile = await economy.profile(GUILD, ALICE)
    assert profile["games"] == 0


async def test_refund_after_settlement_is_a_no_op(economy):
    wager = await economy.place_wager(GUILD, ALICE, 250, "mines")
    await economy.settle(wager, 0)
    await economy.refund(wager)
    assert await economy.balance(GUILD, ALICE) == 750


async def test_merging_a_top_up_keeps_one_round_in_the_history(economy):
    """Doubling down is two debits but must stay a single round."""
    first = await economy.place_wager(GUILD, ALICE, 100, "blackjack")
    top_up = await economy.place_wager(GUILD, ALICE, 100, "blackjack")
    merge_wagers(first, top_up)
    assert first.stake == 200
    await economy.settle(first, 400)
    profile = await economy.profile(GUILD, ALICE)
    assert profile["games"] == 1
    assert profile["wagered"] == 200
    assert await economy.balance(GUILD, ALICE) == 1_200


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------


async def test_statistics_follow_the_rounds_played(economy):
    for payout in (0, 200, 0, 400):
        wager = await economy.place_wager(GUILD, ALICE, 100, "slots")
        await economy.settle(wager, payout)
    profile = await economy.profile(GUILD, ALICE)
    assert profile["games"] == 4
    assert profile["wins"] == 2
    assert profile["losses"] == 2
    assert profile["wagered"] == 400
    assert profile["net"] == -100 + 100 - 100 + 300
    assert profile["biggest_win"] == 300
    assert profile["biggest_loss"] == 100


async def test_win_streaks_grow_and_reset(economy):
    for payout in (200, 200, 200):
        wager = await economy.place_wager(GUILD, ALICE, 100, "dice")
        await economy.settle(wager, payout)
    assert (await economy.profile(GUILD, ALICE))["streak"] == 3
    wager = await economy.place_wager(GUILD, ALICE, 100, "dice")
    await economy.settle(wager, 0)
    profile = await economy.profile(GUILD, ALICE)
    assert profile["streak"] == 0
    assert profile["best_streak"] == 3


async def test_a_push_does_not_break_a_streak(economy):
    for payout in (200, 100, 200):  # win, push, win
        wager = await economy.place_wager(GUILD, ALICE, 100, "dice")
        await economy.settle(wager, payout)
    assert (await economy.profile(GUILD, ALICE))["streak"] == 2


async def test_favourite_game_is_the_one_played_most(economy):
    """It used to be whichever game was played last, which is not the same."""
    for _ in range(3):
        wager = await economy.place_wager(GUILD, ALICE, 10, "slots")
        await economy.settle(wager, 0)
    wager = await economy.place_wager(GUILD, ALICE, 10, "roulette")
    await economy.settle(wager, 0)
    assert await economy.favourite_game(GUILD, ALICE) == "slots"


# ---------------------------------------------------------------------------
# transfers
# ---------------------------------------------------------------------------


async def test_a_transfer_conserves_chips(economy):
    before = await economy.balance(GUILD, ALICE) + await economy.balance(GUILD, BOB)
    sender, recipient = await economy.transfer(GUILD, ALICE, BOB, 300)
    assert (sender, recipient) == (700, 1_300)
    assert sender + recipient == before


async def test_an_unaffordable_transfer_moves_nothing(economy):
    with pytest.raises(InsufficientFunds):
        await economy.transfer(GUILD, ALICE, BOB, 5_000)
    assert await economy.balance(GUILD, ALICE) == 1_000
    assert await economy.balance(GUILD, BOB) == 1_000


async def test_concurrent_transfers_cannot_conjure_chips(economy):
    await asyncio.gather(
        *(economy.transfer(GUILD, ALICE, BOB, 300) for _ in range(10)),
        return_exceptions=True,
    )
    total = await economy.balance(GUILD, ALICE) + await economy.balance(GUILD, BOB)
    assert total == 2_000


# ---------------------------------------------------------------------------
# faucets
# ---------------------------------------------------------------------------


async def test_daily_pays_once_per_calendar_day(economy, settings):
    amount, streak = await economy.claim_daily(GUILD, ALICE, settings)
    assert amount == settings["daily_amount"]
    assert streak == 1
    with pytest.raises(OnCooldown):
        await economy.claim_daily(GUILD, ALICE, settings)


async def test_daily_streak_bonus_grows_and_then_caps(economy, settings):
    await economy.claim_daily(GUILD, ALICE, settings)
    base = int(settings["daily_amount"])
    for day in range(2, 15):
        # Pretend the last claim was yesterday so the streak continues.
        yesterday = (utcnow() - timedelta(days=1)).date().isoformat()
        await economy.db.execute(
            "UPDATE players SET last_daily_date = ? WHERE guild_id = ? AND user_id = ?",
            (yesterday, GUILD, ALICE),
        )
        amount, streak = await economy.claim_daily(GUILD, ALICE, settings)
        assert streak == day
        assert amount <= base * 2  # the bonus is capped at double
    assert amount == base * 2


async def test_a_missed_day_restarts_the_streak(economy, settings):
    await economy.claim_daily(GUILD, ALICE, settings)
    long_ago = (utcnow() - timedelta(days=5)).date().isoformat()
    await economy.db.execute(
        "UPDATE players SET last_daily_date = ? WHERE guild_id = ? AND user_id = ?",
        (long_ago, GUILD, ALICE),
    )
    _, streak = await economy.claim_daily(GUILD, ALICE, settings)
    assert streak == 1


async def test_work_respects_its_cooldown(economy, settings):
    earned, balance, multiplier = await economy.work(GUILD, ALICE, settings)
    assert settings["work_min"] <= earned <= settings["work_max"]
    assert multiplier == 1.0
    with pytest.raises(OnCooldown):
        await economy.work(GUILD, ALICE, settings)


async def test_work_pays_more_at_a_higher_job_level(economy, settings):
    await economy.set_work_level(GUILD, ALICE, 4)
    earned, _, multiplier = await economy.work(GUILD, ALICE, settings)
    assert multiplier == 1.0 + 4 * settings["upgrade_multiplier_step"]
    assert earned >= int(settings["work_min"] * multiplier)


async def test_clearing_cooldowns_lets_a_player_claim_again(economy, settings):
    await economy.work(GUILD, ALICE, settings)
    await economy.clear_cooldowns(GUILD, ALICE)
    await economy.work(GUILD, ALICE, settings)  # must not raise


# ---------------------------------------------------------------------------
# job upgrades
# ---------------------------------------------------------------------------


def test_upgrade_costs_rise_geometrically(settings):
    costs = [describe_upgrade(settings, level)["cost"] for level in range(6)]
    assert costs == sorted(costs)
    growth = settings["upgrade_cost_growth"]
    for earlier, later in zip(costs, costs[1:]):
        assert later == pytest.approx(earlier * growth, rel=0.01)


def test_each_upgrade_adds_a_fixed_step_to_the_multiplier(settings):
    step = settings["upgrade_multiplier_step"]
    for level in range(8):
        upgrade = describe_upgrade(settings, level)
        assert upgrade["multiplier"] == pytest.approx(1 + step * (level + 1))


def test_upgrades_never_run_out_of_names(settings):
    assert describe_upgrade(settings, 200)["name"]


async def test_buying_an_upgrade_charges_exactly_the_displayed_price(economy, settings):
    """The price shown and the price charged come from one function."""
    quoted = describe_upgrade(settings, 0)
    await economy.set_balance(GUILD, ALICE, quoted["cost"] + 10)
    bought, balance = await economy.buy_upgrade(GUILD, ALICE, settings)
    assert bought["cost"] == quoted["cost"]
    assert balance == 10


async def test_an_unaffordable_upgrade_changes_nothing(economy, settings):
    await economy.set_balance(GUILD, ALICE, 1)
    with pytest.raises(InsufficientFunds):
        await economy.buy_upgrade(GUILD, ALICE, settings)
    profile = await economy.profile(GUILD, ALICE)
    assert profile["work_level"] == 0
    assert profile["balance"] == 1


# ---------------------------------------------------------------------------
# history and leaderboards
# ---------------------------------------------------------------------------


async def test_every_movement_lands_in_the_ledger(economy, settings):
    await economy.claim_daily(GUILD, ALICE, settings)
    wager = await economy.place_wager(GUILD, ALICE, 100, "slots")
    await economy.settle(wager, 0)
    await economy.transfer(GUILD, ALICE, BOB, 50)
    rows = await economy.history(GUILD, ALICE, limit=10)
    assert [row["kind"] for row in rows] == ["transfer", "game", "daily"]
    assert rows[-1]["delta"] == settings["daily_amount"]


async def test_history_paginates(economy):
    for _ in range(12):
        wager = await economy.place_wager(GUILD, ALICE, 10, "dice")
        await economy.settle(wager, 0)
    assert await economy.history_count(GUILD, ALICE) == 12
    first = await economy.history(GUILD, ALICE, limit=5, offset=0)
    second = await economy.history(GUILD, ALICE, limit=5, offset=5)
    assert len(first) == len(second) == 5
    assert {row["id"] for row in first}.isdisjoint({row["id"] for row in second})


async def test_leaderboards_rank_by_the_requested_metric(economy):
    await economy.set_balance(GUILD, ALICE, 10_000)
    await economy.set_balance(GUILD, BOB, 50_000)
    board = await economy.leaderboard(GUILD, "balance")
    assert [row["user_id"] for row in board] == [BOB, ALICE]

    wager = await economy.place_wager(GUILD, ALICE, 5_000, "slots")
    await economy.settle(wager, 0)
    board = await economy.leaderboard(GUILD, "wagered")
    assert board[0]["user_id"] == ALICE


async def test_windowed_leaderboard_only_counts_recent_play(economy):
    wager = await economy.place_wager(GUILD, ALICE, 500, "slots")
    await economy.settle(wager, 0)
    # Backdate that round beyond the window.
    await economy.db.execute(
        "UPDATE ledger SET created_at = ? WHERE guild_id = ?",
        ((utcnow() - timedelta(days=10)).isoformat(), GUILD),
    )
    recent = await economy.place_wager(GUILD, BOB, 700, "dice")
    await economy.settle(recent, 0)

    board = await economy.windowed_leaderboard(GUILD, "wagered", utcnow() - timedelta(days=1))
    assert [row["user_id"] for row in board] == [BOB]
    assert board[0]["value"] == 700


async def test_wagered_since_only_counts_stakes(economy, settings):
    await economy.claim_daily(GUILD, ALICE, settings)  # not a stake
    wager = await economy.place_wager(GUILD, ALICE, 300, "slots")
    await economy.settle(wager, 0)
    assert await economy.wagered_since(GUILD, ALICE, utcnow() - timedelta(hours=1)) == 300


async def test_guild_totals_summarise_the_whole_server(economy):
    for game, stake in (("slots", 100), ("dice", 200), ("slots", 300)):
        wager = await economy.place_wager(GUILD, ALICE, stake, game)
        await economy.settle(wager, 0)
    totals = await economy.guild_totals(GUILD)
    assert totals["players"] == 2
    assert totals["games"] == 3
    assert totals["wagered"] == 600
    assert totals["games_by_popularity"][0]["game"] == "slots"


# ---------------------------------------------------------------------------
# challenges
# ---------------------------------------------------------------------------


async def test_challenge_progress_accumulates_from_play(economy):
    for _ in range(3):
        wager = await economy.place_wager(GUILD, ALICE, 100, "slots")
        await economy.settle(wager, 200)
    daily = await economy.challenge(GUILD, ALICE, "daily")
    assert daily["games"] == 3
    assert daily["wagered"] == 300
    assert daily["wins"] == 3


async def test_challenge_progress_resets_when_the_period_rolls_over(economy):
    wager = await economy.place_wager(GUILD, ALICE, 100, "slots")
    await economy.settle(wager, 0)
    await economy.db.execute(
        "UPDATE challenge_progress SET period_key = 'ancient' WHERE guild_id = ?", (GUILD,)
    )
    fresh = await economy.challenge(GUILD, ALICE, "daily")
    assert fresh["games"] == 0


async def test_a_challenge_reward_can_only_be_claimed_once(economy):
    from casino.db.economy import AlreadyClaimed

    wager = await economy.place_wager(GUILD, ALICE, 100, "slots")
    await economy.settle(wager, 0)
    balance = await economy.claim_challenge(GUILD, ALICE, "daily", 500, "Test")
    assert balance == 900 + 500
    with pytest.raises(AlreadyClaimed):
        await economy.claim_challenge(GUILD, ALICE, "daily", 500, "Test")


async def test_concurrent_claims_pay_out_once(economy):
    from casino.db.economy import AlreadyClaimed

    wager = await economy.place_wager(GUILD, ALICE, 100, "slots")
    await economy.settle(wager, 0)
    results = await asyncio.gather(
        *(economy.claim_challenge(GUILD, ALICE, "daily", 500, "Test") for _ in range(5)),
        return_exceptions=True,
    )
    paid = [r for r in results if not isinstance(r, Exception)]
    assert len(paid) == 1
    assert all(isinstance(r, AlreadyClaimed) for r in results if isinstance(r, Exception))


# ---------------------------------------------------------------------------
# resets
# ---------------------------------------------------------------------------


async def test_resetting_a_player_clears_everything_but_self_exclusion(db, economy):
    progression = Progression(db)
    wager = await economy.place_wager(GUILD, ALICE, 500, "slots")
    await economy.settle(wager, 0)
    await progression.award_xp(GUILD, ALICE, 5_000)
    until = await progression.set_self_exclusion(GUILD, ALICE, timedelta(days=7))

    await economy.reset_player(GUILD, ALICE, 2_500)

    profile = await economy.profile(GUILD, ALICE)
    state = await progression.get(GUILD, ALICE)
    assert profile["balance"] == 2_500
    assert profile["games"] == 0
    assert state["level"] == 1
    assert state["xp"] == 0
    # The point of self-exclusion is that nobody can shorten it.
    assert state["self_exclude_until"] == until.isoformat()


async def test_resetting_one_player_leaves_the_others_alone(economy):
    await economy.set_balance(GUILD, BOB, 9_999)
    await economy.reset_player(GUILD, ALICE, 2_500)
    assert await economy.balance(GUILD, BOB) == 9_999


# ---------------------------------------------------------------------------
# guild settings
# ---------------------------------------------------------------------------


async def test_settings_fall_back_to_defaults(db):
    store = GuildStore(db)
    settings = await store.settings(GUILD)
    assert settings["min_bet"] == GUILD_DEFAULTS["min_bet"]


async def test_settings_round_trip_and_stay_cached(db):
    store = GuildStore(db)
    await store.update(GUILD, min_bet=50, max_bet=5_000)
    assert (await store.settings(GUILD))["min_bet"] == 50
    fresh = GuildStore(db)  # a second process would see the same thing
    assert (await fresh.settings(GUILD))["max_bet"] == 5_000


async def test_unknown_settings_are_refused(db):
    store = GuildStore(db)
    with pytest.raises(UnknownSetting):
        await store.update(GUILD, definitely_not_a_setting=1)


async def test_games_can_be_switched_off_per_guild(db):
    store = GuildStore(db)
    assert await store.game_enabled(GUILD, "slots") is True
    await store.set_game(GUILD, "slots", False)
    assert await store.game_enabled(GUILD, "slots") is False
    assert await store.game_enabled(GUILD, "dice") is True
    assert await store.game_enabled(None, "slots") is True  # DMs are unrestricted


async def test_log_categories_can_be_switched_off_per_guild(db):
    store = GuildStore(db)
    await store.set_log(GUILD, "messages", False)
    logs = await store.logs(GUILD)
    assert logs["messages"] is False
    assert logs["moderation"] is True


async def test_resetting_settings_restores_defaults(db):
    store = GuildStore(db)
    await store.update(GUILD, min_bet=999)
    await store.reset(GUILD)
    assert (await store.settings(GUILD))["min_bet"] == GUILD_DEFAULTS["min_bet"]


async def test_two_racing_settlements_pay_a_round_out_once(economy):
    """Found by the crash-game flow test: the "already settled" check used to
    sit before an await, so two callers could both slip past it and credit the
    same winning round twice."""
    wager = await economy.place_wager(GUILD, ALICE, 100, "crash")
    results = await asyncio.gather(
        *(economy.settle(wager, 1_000) for _ in range(4)),
        return_exceptions=True,
    )
    settled = [r for r in results if not isinstance(r, Exception)]
    assert len(settled) == 1
    assert await economy.balance(GUILD, ALICE) == 900 + 1_000
    profile = await economy.profile(GUILD, ALICE)
    assert profile["games"] == 1
