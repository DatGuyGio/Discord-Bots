"""The bet funnel: amount parsing and every rejection rule.

The old bot validated bets in three places and the button paths skipped the
maximum-bet check entirely, which is how a 999,999,999,999,999,999,999,999,999
chip blackjack hand ended up in the saved data. Everything now goes through
:class:`casino.core.guard.RoundGuard`, so these tests cover *all* entry
points at once — slash commands, buttons, modals and replays alike.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio

from casino.core.amounts import parse_amount
from casino.core.errors import (
    BetOutOfRange,
    CasinoBanned,
    GameDisabled,
    InsufficientFunds,
    InvalidAmount,
    Maintenance,
    SelfExcluded,
    WagerLimitReached,
)
from casino.core.guard import RoundGuard
from casino.db.database import Database
from casino.db.economy import Economy, utcnow
from casino.db.guilds import GuildStore
from casino.db.progression import Progression

GUILD = 500
PLAYER = 900


@pytest_asyncio.fixture
async def guard(tmp_path):
    db = await Database(str(tmp_path / "guard.sqlite3")).connect()
    economy = Economy(db)
    guilds = GuildStore(db)
    progression = Progression(db)
    await guilds.update(GUILD, min_bet=10, max_bet=1_000, starting_balance=5_000)
    await economy.ensure(GUILD, PLAYER, 5_000)
    instance = RoundGuard(economy, guilds, progression)
    instance.economy = economy  # convenience for the tests below
    yield instance
    await db.close()


# ---------------------------------------------------------------------------
# amount parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("100", 100),
        ("1,000", 1_000),
        ("1_000", 1_000),
        (" 250 ", 250),
        ("1k", 1_000),
        ("2.5k", 2_500),
        ("3m", 3_000_000),
        ("1b", 1_000_000_000),
        ("all", 4_000),
        ("max", 4_000),
        ("half", 2_000),
        ("quarter", 1_000),
        ("25%", 1_000),
        ("10%", 400),
    ],
)
def test_amounts_people_actually_type(text, expected):
    assert parse_amount(text, balance=4_000) == expected


@pytest.mark.parametrize("text", ["", "abc", "-5", "0", "1.2.3", "5x", "%", "200%", None, "1e9"])
def test_nonsense_amounts_are_rejected(text):
    with pytest.raises(InvalidAmount):
        parse_amount(text, balance=1_000)


def test_an_integer_passes_straight_through():
    """Replay buttons hand the stake back as an int, not a string."""
    assert parse_amount(750, balance=10) == 750


# ---------------------------------------------------------------------------
# stake validation
# ---------------------------------------------------------------------------


async def test_a_valid_stake_is_accepted(guard):
    assert await guard.resolve_stake(GUILD, PLAYER, "500") == 500


async def test_a_stake_below_the_minimum_is_refused(guard):
    with pytest.raises(BetOutOfRange):
        await guard.resolve_stake(GUILD, PLAYER, "5")


async def test_a_stake_above_the_maximum_is_refused(guard):
    """The check the old button and modal paths did not have."""
    with pytest.raises(BetOutOfRange):
        await guard.resolve_stake(GUILD, PLAYER, "1001")


async def test_the_exploit_bet_from_the_old_data_is_now_impossible(guard):
    with pytest.raises(BetOutOfRange):
        await guard.resolve_stake(GUILD, PLAYER, "999999999999999999999999999")


async def test_all_in_is_clamped_by_the_table_maximum(guard):
    """'all' with a 5,000 balance and a 1,000 limit is a rejection, not a
    silent 1,000 bet: the player should know the table has a ceiling."""
    with pytest.raises(BetOutOfRange):
        await guard.resolve_stake(GUILD, PLAYER, "all")


async def test_you_cannot_stake_chips_you_do_not_have(guard):
    await guard.economy.set_balance(GUILD, PLAYER, 100)
    with pytest.raises(InsufficientFunds):
        await guard.resolve_stake(GUILD, PLAYER, "500")


async def test_a_caller_can_lower_the_minimum(guard):
    """``/give`` allows amounts below the table minimum."""
    assert await guard.resolve_stake(GUILD, PLAYER, "1", minimum=1) == 1


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------


async def test_the_casino_is_open_by_default(guard):
    await guard.check_open(GUILD, PLAYER)  # must not raise


async def test_maintenance_closes_the_tables(guard):
    await guard.guilds.update(GUILD, maintenance=1)
    with pytest.raises(Maintenance):
        await guard.check_open(GUILD, PLAYER)


async def test_a_casino_ban_blocks_play_until_it_expires(guard):
    await guard.progression.set_casino_ban(GUILD, PLAYER, utcnow() + timedelta(hours=1))
    with pytest.raises(CasinoBanned):
        await guard.check_open(GUILD, PLAYER)

    await guard.progression.set_casino_ban(GUILD, PLAYER, utcnow() - timedelta(hours=1))
    await guard.check_open(GUILD, PLAYER)  # expired bans do not block


async def test_self_exclusion_blocks_play(guard):
    await guard.progression.set_self_exclusion(GUILD, PLAYER, timedelta(days=1))
    with pytest.raises(SelfExcluded):
        await guard.check_open(GUILD, PLAYER)


async def test_self_exclusion_cannot_be_shortened(guard):
    """It is the one setting the bot refuses to relax, by design."""
    long_lock = await guard.progression.set_self_exclusion(GUILD, PLAYER, timedelta(days=30))
    attempted = await guard.progression.set_self_exclusion(GUILD, PLAYER, timedelta(days=1))
    assert attempted == long_lock


async def test_a_daily_wager_limit_stops_play_once_reached(guard):
    await guard.progression.set_wager_limit(GUILD, PLAYER, 1_000)
    wager = await guard.economy.place_wager(GUILD, PLAYER, 1_000, "slots")
    await guard.economy.settle(wager, 0)
    with pytest.raises(WagerLimitReached):
        await guard.check_open(GUILD, PLAYER)


async def test_a_wager_limit_only_counts_today(guard):
    await guard.progression.set_wager_limit(GUILD, PLAYER, 1_000)
    wager = await guard.economy.place_wager(GUILD, PLAYER, 1_000, "slots")
    await guard.economy.settle(wager, 0)
    await guard.economy.db.execute(
        "UPDATE ledger SET created_at = ? WHERE guild_id = ?",
        ((utcnow() - timedelta(days=2)).isoformat(), GUILD),
    )
    await guard.check_open(GUILD, PLAYER)  # yesterday's staking does not count


async def test_a_disabled_game_is_refused(guard):
    await guard.guilds.set_game(GUILD, "slots", False)
    with pytest.raises(GameDisabled):
        await guard.check_game(GUILD, "slots")
    await guard.check_game(GUILD, "dice")


# ---------------------------------------------------------------------------
# the funnel itself
# ---------------------------------------------------------------------------


async def test_opening_a_round_debits_the_stake(guard):
    wager = await guard.open(GUILD, PLAYER, "slots", "500")
    assert wager.stake == 500
    assert await guard.economy.balance(GUILD, PLAYER) == 4_500


async def test_a_rejected_round_never_touches_the_balance(guard):
    for stake in ("1", "999999", "abc"):
        with pytest.raises(Exception):
            await guard.open(GUILD, PLAYER, "slots", stake)
    assert await guard.economy.balance(GUILD, PLAYER) == 5_000


async def test_availability_is_checked_before_the_amount(guard):
    """A self-excluded player is told why, not lectured about bet sizing."""
    await guard.progression.set_self_exclusion(GUILD, PLAYER, timedelta(days=1))
    with pytest.raises(SelfExcluded):
        await guard.open(GUILD, PLAYER, "slots", "999999")


async def test_a_new_player_is_created_on_first_contact(guard):
    settings = await guard.ensure_player(GUILD, 4242)
    assert await guard.economy.balance(GUILD, 4242) == int(settings["starting_balance"])


async def test_the_guild_rtp_is_what_games_are_paid_at(guard):
    await guard.guilds.update(GUILD, rtp=0.9)
    assert await guard.rtp(GUILD) == 0.9


# ---------------------------------------------------------------------------
# timers
# ---------------------------------------------------------------------------


async def test_daily_timer_reports_readiness_then_a_wait(guard):
    settings = await guard.guilds.settings(GUILD)
    assert await guard.time_until_daily(GUILD, PLAYER) is None
    await guard.economy.claim_daily(GUILD, PLAYER, settings)
    remaining = await guard.time_until_daily(GUILD, PLAYER)
    assert remaining is not None and remaining <= timedelta(days=1)


async def test_work_timer_matches_the_configured_cooldown(guard):
    await guard.guilds.update(GUILD, work_cooldown=600)
    settings = await guard.guilds.settings(GUILD)
    assert await guard.time_until_work(GUILD, PLAYER) is None
    await guard.economy.work(GUILD, PLAYER, settings)
    remaining = await guard.time_until_work(GUILD, PLAYER)
    assert remaining is not None and remaining <= timedelta(seconds=600)
