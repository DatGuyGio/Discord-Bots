"""Importing the old JSON data.

The fixtures below are the *real* saved data from the running bot, trimmed
down but otherwise unchanged — including the 10^27 wager that an unbounded
bet produced, the ``max_bet: 0`` that allowed it, and the legacy
non-guild-scoped keys left over from before per-server balances existed.

If this passes, the switch to SQLite costs nobody their chips.
"""

from __future__ import annotations

import json

import pytest_asyncio

from casino.config import MAX_CHIPS
from casino.db.database import Database
from casino.db.economy import Economy
from casino.db.guilds import GuildStore
from casino.db.migrate import migrate
from casino.db.progression import Progression

GUILD_A = 1519698709418344458
GUILD_B = 1284926958140002375
WHALE = 1077246894788587622
CASUAL = 485021038225391616

BALANCES = {
    f"{GUILD_A}:{WHALE}": {
        "balance": 693146,
        "last_daily": "2026-09-15T18:10:33.858632+00:00",
        "last_work": "2026-09-15T18:11:01.413131+00:00",
        "work_level": 100,
        "games_played": 11,
        # The scar: an unbounded blackjack bet.
        "total_wagered": 1000000000000000000011701931,
        "net_profit": -1000000000000000000010571941,
        "biggest_win": 376380,
        "biggest_loss": 999999999999999999999999999,
        "games_won": 4,
        "games_lost": 7,
        "current_streak": 0,
        "best_streak": 2,
        "favorite_game": "Blackjack",
        "daily_streak": 1,
        "last_daily_date": "2026-09-15",
        "achievements": ["big_win", "millionaire", "first_game"],
        "transactions": [
            {"type": "daily", "amount": 500, "timestamp": "2026-09-13T17:03:51.154606+00:00", "note": "Day 1 streak"},
            {"type": "roulette", "wagered": 200, "net": -200, "timestamp": "2026-09-13T17:04:10.301415+00:00"},
            {"type": "blackjack", "wagered": 999999999999999999999999999,
             "net": -999999999999999999999999999, "timestamp": "2026-09-13T17:37:56.419658+00:00"},
            {"type": "blackjack", "wagered": 376380, "net": 376380, "timestamp": "2026-09-13T17:45:17.373014+00:00"},
        ],
        "last_game": "Blackjack",
        "last_bet": 376380,
        "last_roulette_bet": "odd",
        "daily_challenge": {"date": "2026-09-15", "games": 1, "wagered": 376380, "wins": 0, "work": 1, "claimed": False},
        "weekly_challenge": {"week": "2026-W38", "games": 1, "wagered": 376380, "wins": 0, "work": 1, "claimed": False},
    },
    f"{GUILD_A}:{CASUAL}": {
        "balance": 5300, "work_level": 0, "games_played": 1, "total_wagered": 200,
        "net_profit": 300, "biggest_win": 300, "games_won": 1, "games_lost": 0,
        "current_streak": 1, "best_streak": 1, "favorite_game": "Blackjack",
        "achievements": ["first_game"], "last_game": "Blackjack", "last_bet": 200,
        "transactions": [
            {"type": "blackjack", "wagered": 200, "net": 300, "timestamp": "2026-09-13T17:45:17.379654+00:00"}
        ],
    },
    # Pre-guild-scoping leftover. The old bot copied these into each guild on
    # startup, so the guild-scoped rows above are the real data.
    str(WHALE): {"balance": 1000, "work_level": 0, "games_played": 0, "transactions": []},
}

ECONOMY = {
    str(GUILD_A): {
        "starting_balance": 5000, "min_bet": 100,
        "max_bet": 0,  # "unlimited" — the setting that allowed the 10^27 bet
        "daily_amount": 25000, "work_min": 500, "work_max": 10000, "work_cooldown": 15,
        "upgrade_base_cost": 2200, "upgrade_cost_growth": 1.3, "upgrade_multiplier_step": 0.5,
        "enabled_games": {"roulette": True, "blackjack": True, "slots": False, "coinflip": True,
                          "dice": True, "mines": True, "duel": True},
    },
    str(GUILD_B): {
        "starting_balance": 1000, "min_bet": 10, "max_bet": 1000000,
        "daily_amount": 500, "work_min": 50, "work_max": 150, "work_cooldown": 1800,
        "enabled_games": {"roulette": True, "blackjack": True, "slots": True},
    },
}

FEATURES = {
    "guilds": {
        str(GUILD_B): {
            "welcome_channel": None,
            "logs_channel": 1551187160600936549,
            "logs_enabled": {"messages": True, "members": True, "moderation": True},
            "ticket_message": "Thanks for opening a ticket.",
            "casino_maintenance": True,
        }
    },
    "tickets": {},
}

PROGRESS = {
    f"{GUILD_A}:{WHALE}": {
        "xp": 1284, "level": 14, "prestige": 0, "title": None,
        "owned_titles": ["title_winner"], "owned_cosmetics": ["cosmetic_gold"],
        "casino_ban_until": None, "self_exclude_until": None, "suspicion": 0,
    },
    f"{GUILD_A}:{CASUAL}": {"xp": 25, "level": 1, "prestige": 0, "owned_titles": []},
}

MODERATION = {
    "log_channel_id": 1551187160600936549,
    "warnings": {str(CASUAL): [{"moderator_id": WHALE, "reason": "spam", "timestamp": "2026-09-14T10:00:00+00:00"}]},
}


@pytest_asyncio.fixture
async def imported(tmp_path):
    for name, payload in (
        ("balances.json", BALANCES),
        ("casino_economy.json", ECONOMY),
        ("bot_features.json", FEATURES),
        ("casino_progress.json", PROGRESS),
        ("moderation.json", MODERATION),
    ):
        (tmp_path / name).write_text(json.dumps(payload))

    db = await Database(str(tmp_path / "imported.sqlite3")).connect()
    report = await migrate(tmp_path, db)
    yield db, report
    await db.close()


# ---------------------------------------------------------------------------
# players
# ---------------------------------------------------------------------------


async def test_balances_survive_the_move(imported):
    db, _ = imported
    economy = Economy(db)
    assert await economy.balance(GUILD_A, WHALE) == 693_146
    assert await economy.balance(GUILD_A, CASUAL) == 5_300


async def test_cooldowns_and_streaks_survive(imported):
    db, _ = imported
    profile = await Economy(db).profile(GUILD_A, WHALE)
    assert profile["work_level"] == 100
    assert profile["daily_streak"] == 1
    assert profile["last_daily_date"] == "2026-09-15"
    assert profile["last_work"].startswith("2026-09-15")


async def test_absurd_values_are_clamped_and_reported(imported):
    """10^27 breaks formatting and makes every leaderboard meaningless."""
    db, report = imported
    profile = await Economy(db).profile(GUILD_A, WHALE)
    assert profile["wagered"] == MAX_CHIPS
    assert profile["net"] == -MAX_CHIPS
    assert profile["biggest_loss"] == MAX_CHIPS
    assert any("total_wagered" in entry for entry in report.clamped)


async def test_sane_values_are_left_exactly_as_they_were(imported):
    db, _ = imported
    profile = await Economy(db).profile(GUILD_A, CASUAL)
    assert profile["wagered"] == 200
    assert profile["net"] == 300
    assert profile["biggest_win"] == 300
    assert profile["games"] == 1
    assert profile["wins"] == 1


async def test_transaction_history_becomes_ledger_rows(imported):
    db, _ = imported
    rows = await Economy(db).history(GUILD_A, WHALE, limit=50)
    assert len(rows) == 4
    kinds = {row["kind"] for row in rows}
    assert kinds == {"game", "daily"}
    assert all(abs(int(row["delta"])) <= MAX_CHIPS for row in rows)


async def test_game_history_produces_a_real_favourite_game(imported):
    db, _ = imported
    assert await Economy(db).favourite_game(GUILD_A, WHALE) == "blackjack"


async def test_challenge_progress_survives(imported):
    db, _ = imported
    daily = await Economy(db).challenge(GUILD_A, WHALE, "daily")
    # The stored period key is a past date, so today's progress starts clean.
    assert daily["games"] == 0
    row = await db.fetchone(
        "SELECT * FROM challenge_progress WHERE guild_id = ? AND user_id = ? AND period_kind = 'daily'",
        (GUILD_A, WHALE),
    )
    assert row["period_key"] == "2026-09-15"
    assert row["wagered"] == 376_380


async def test_achievements_survive(imported):
    db, _ = imported
    unlocked = await Progression(db).unlocked(GUILD_A, WHALE)
    assert {"big_win", "millionaire", "first_game"} <= unlocked


async def test_legacy_global_keys_are_skipped_not_duplicated(imported):
    """Importing them would hand out a second, unearned balance."""
    db, report = imported
    rows = await db.fetchall("SELECT guild_id, user_id FROM players")
    assert len(rows) == 2
    assert any("legacy global balance key" in entry for entry in report.skipped)


# ---------------------------------------------------------------------------
# progression
# ---------------------------------------------------------------------------


async def test_levels_and_xp_survive(imported):
    db, _ = imported
    state = await Progression(db).get(GUILD_A, WHALE)
    assert state["level"] == 14
    assert state["xp"] == 1_284


async def test_owned_cosmetics_are_remapped_to_the_new_shop(imported):
    db, _ = imported
    owned = await Progression(db).inventory(GUILD_A, WHALE)
    assert owned == {"title_rookie", "badge_gold"}


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


async def test_economy_settings_survive(imported):
    db, _ = imported
    settings = await GuildStore(db).settings(GUILD_A)
    assert settings["starting_balance"] == 5_000
    assert settings["min_bet"] == 100
    assert settings["daily_amount"] == 25_000
    assert settings["work_cooldown"] == 15


async def test_unlimited_max_bet_becomes_a_real_ceiling(imported):
    """Carrying `max_bet: 0` across would carry the exploit across with it."""
    db, report = imported
    settings = await GuildStore(db).settings(GUILD_A)
    assert settings["max_bet"] > 0
    assert any("max_bet 0" in entry for entry in report.skipped)


async def test_disabled_games_stay_disabled(imported):
    db, _ = imported
    store = GuildStore(db)
    assert await store.game_enabled(GUILD_A, "slots") is False
    assert await store.game_enabled(GUILD_A, "roulette") is True
    # A game the old config never mentioned defaults to on.
    assert await store.game_enabled(GUILD_A, "crash") is True


async def test_feature_settings_and_maintenance_survive(imported):
    db, _ = imported
    settings = await GuildStore(db).settings(GUILD_B)
    assert settings["logs_channel"] == 1551187160600936549
    assert settings["maintenance"] == 1
    assert settings["ticket_message"] == "Thanks for opening a ticket."


async def test_log_toggles_survive(imported):
    db, _ = imported
    logs = await GuildStore(db).logs(GUILD_B)
    assert logs["messages"] is True
    assert logs["moderation"] is True


async def test_global_warnings_land_in_each_guild(imported):
    db, report = imported
    from casino.db.support import Moderation

    warnings = await Moderation(db).warnings(GUILD_A, CASUAL)
    assert len(warnings) == 1
    assert warnings[0]["reason"] == "spam"
    assert report.warnings >= 1


# ---------------------------------------------------------------------------
# safety
# ---------------------------------------------------------------------------


async def test_importing_twice_changes_nothing(imported, tmp_path):
    """Re-running after an interrupted import must not double anyone's chips."""
    db, _ = imported
    before = await Economy(db).balance(GUILD_A, WHALE)
    rows_before = await db.fetchval("SELECT COUNT(*) FROM players")

    await migrate(tmp_path, db)

    assert await Economy(db).balance(GUILD_A, WHALE) == before
    assert await db.fetchval("SELECT COUNT(*) FROM players") == rows_before


async def test_a_missing_file_is_not_fatal(tmp_path):
    """Someone might only have balances.json, or nothing at all."""
    db = await Database(str(tmp_path / "empty.sqlite3")).connect()
    try:
        report = await migrate(tmp_path, db)
        assert report.players == 0
    finally:
        await db.close()


async def test_corrupt_json_is_skipped_rather_than_crashing(tmp_path):
    (tmp_path / "balances.json").write_text("{not json at all")
    db = await Database(str(tmp_path / "corrupt.sqlite3")).connect()
    try:
        report = await migrate(tmp_path, db)
        assert report.players == 0
    finally:
        await db.close()


async def test_the_import_is_recorded_so_it_is_obvious_it_ran(imported):
    db, _ = imported
    assert await db.get_meta("json_import_at") is not None


async def test_the_report_reads_like_a_summary(imported):
    _, report = imported
    text = report.render()
    assert "players imported" in text
    assert "clamped values" in text
