"""Wiring: does the bot actually assemble?

Most of what breaks a Discord bot in production is not game logic, it is
deployment: a cog that fails to import, a slash command whose name Discord
rejects, two commands claiming the same name, a persistent view with more
than 25 components, or a button whose ``custom_id`` was never registered so
it silently stops working after a restart.

The old single-file bot could not be checked for any of this without a token
and a live connection. :meth:`CasinoBot.prepare` builds everything except the
gateway connection, so all of it is checkable here in milliseconds.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from discord.ext import commands

from casino.config import GAME_KEYS, Settings
from casino.core.bot import COGS, CasinoBot
from casino.ui import theme
from casino.ui.hub import HubView, hub_embed
from casino.ui.views import Paginator

NAME_RULE = re.compile(r"^[a-z0-9_-]{1,32}$")


@pytest_asyncio.fixture
async def bot(tmp_path):
    instance = CasinoBot(Settings(token="dummy", database_path=str(tmp_path / "bot.sqlite3")))
    await instance.prepare()
    yield instance
    await instance.db.close()


def fake_user(user_id: int = 42, guild_id: int | None = 7) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        display_name="Tester",
        mention=f"<@{user_id}>",
        display_avatar=SimpleNamespace(url="https://example.invalid/a.png"),
        guild=SimpleNamespace(id=guild_id) if guild_id else None,
    )


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


async def test_every_cog_loads(bot):
    loaded = set(bot.extensions)
    assert loaded == set(COGS), f"missing: {set(COGS) - loaded}"


async def test_storage_is_wired_up(bot):
    for attribute in ("economy", "guilds_store", "progression", "moderation", "tickets", "guard"):
        assert getattr(bot, attribute) is not None


async def test_the_schema_is_applied(bot):
    tables = {
        row["name"]
        for row in await bot.db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    expected = {
        "players", "stats", "game_plays", "ledger", "progression", "inventory",
        "achievements", "challenge_progress", "guild_settings", "guild_games",
        "guild_logs", "warnings", "tickets", "meta",
    }
    assert expected <= tables


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def all_commands(bot: CasinoBot) -> list[commands.Command]:
    return list(bot.walk_commands())


async def test_no_two_commands_share_a_name_or_alias(bot):
    seen: dict[str, str] = {}
    for command in all_commands(bot):
        for name in [command.qualified_name, *(f"{command.full_parent_name} {a}".strip() for a in command.aliases)]:
            assert name not in seen, f"{name} is claimed by both {seen.get(name)} and {command.qualified_name}"
            seen[name] = command.qualified_name


async def test_slash_command_names_are_valid(bot):
    """Discord rejects uppercase, spaces and long names at sync time."""
    for command in bot.tree.walk_commands():
        assert NAME_RULE.match(command.name), f"{command.name!r} would be rejected by Discord"


async def test_every_slash_command_has_a_description(bot):
    """A missing description is a hard error when the tree is synced."""
    for command in bot.tree.walk_commands():
        if isinstance(command, discord.app_commands.Group):
            assert command.description
            continue
        assert command.description and command.description != "…", command.name


async def test_command_descriptions_fit_discords_limit(bot):
    for command in bot.tree.walk_commands():
        assert len(command.description) <= 100, command.name


async def test_the_commands_players_rely_on_exist(bot):
    expected = {
        "casino", "balance", "daily", "work", "jobs", "upgrade", "give", "history",
        "timers", "profile", "achievements", "challenges", "shop", "leaderboard",
        "roulette", "slots", "coinflip", "dice", "mines", "crash", "duel", "blackjack",
        "help", "ping", "verify", "selfexclude", "wagerlimit",
    }
    names = {command.name for command in bot.tree.walk_commands()}
    assert expected <= names, f"missing: {expected - names}"


async def test_every_game_has_a_command(bot):
    names = {command.name for command in bot.tree.walk_commands()}
    for game in GAME_KEYS:
        assert game in names, f"{game} has no command"


async def test_admin_commands_are_gated(bot):
    """Anything that moves other people's chips needs a permission check."""
    protected = {"bank", "resetplayer", "reseteconomy", "config", "maintenance", "casinoban"}
    for command in all_commands(bot):
        if command.qualified_name.split()[0] in protected:
            assert command.checks, f"{command.qualified_name} has no permission check"


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------


async def test_persistent_views_are_registered(bot):
    """A persistent view has to be registered *and* have stable custom ids,
    or its buttons quietly die on the next restart."""
    registered = {
        item.custom_id
        for view in bot.persistent_views
        for item in view.children
        if getattr(item, "custom_id", None)
    }
    assert "casino:hub:slots" in registered
    assert "casino:hub:select" in registered
    assert "casino:ticket:category" in registered
    assert "casino:ticket:close" in registered


async def test_the_hub_fits_inside_discords_component_limits(bot):
    view = HubView(bot)
    assert len(view.children) <= 25
    rows: dict[int, int] = {}
    for item in view.children:
        row = item.row if item.row is not None else 0
        rows[row] = rows.get(row, 0) + 1
    assert all(count <= 5 for count in rows.values()), rows


async def test_the_mines_grid_fits_inside_one_message(bot):
    from casino.games import mines as mines_game
    from casino.games.rng import Fairness
    from casino.ui.games import MinesView

    wager = SimpleNamespace(stake=100, guild_id=7, user_id=42, game="mines", settled=False)
    fairness = Fairness(server_seed="a" * 64)
    board = mines_game.Board.create(3, fairness)
    view = MinesView(bot, fake_user(), wager, board, fairness, play=None)
    assert len(view.children) <= 25
    assert len([child for child in view.children if getattr(child, "index", None) is not None]) == mines_game.TILES


async def test_every_hub_game_button_maps_to_a_handler(bot):
    """The hub dispatches by name; a typo would only show up at click time."""
    games = bot.get_cog("Games")
    blackjack = bot.get_cog("Blackjack")
    for attribute in ("play_slots", "play_mines", "play_crash", "play_coinflip", "play_dice", "play_roulette"):
        assert callable(getattr(games, attribute, None)), attribute
    assert callable(getattr(blackjack, "play_hand", None))
    for cog_name, attribute in (
        ("Economy", "show_wallet"), ("Economy", "do_daily"), ("Economy", "do_work"),
        ("Economy", "show_timers"), ("Progression", "show_profile"),
        ("Progression", "show_challenges"), ("Progression", "show_shop"),
        ("Leaderboards", "show_board"),
    ):
        assert callable(getattr(bot.get_cog(cog_name), attribute, None)), f"{cog_name}.{attribute}"


async def test_paginator_disables_its_arrows_at_the_ends():
    pages = [theme.embed(description=str(index)) for index in range(3)]
    view = Paginator(pages, owner_id=1)
    assert view.previous.disabled and not view.next.disabled
    view.index = 2
    view._sync()
    assert view.next.disabled and not view.previous.disabled
    assert view.counter.label == "3 / 3"


async def test_a_single_page_paginator_has_nothing_to_click():
    view = Paginator([theme.embed(description="only")], owner_id=1)
    assert all(child.disabled for child in view.children)


# ---------------------------------------------------------------------------
# embeds
# ---------------------------------------------------------------------------


async def test_the_hub_embed_renders_for_a_fresh_player(bot):
    settings = await bot.guilds_store.settings(7)
    await bot.economy.ensure(7, 42, int(settings["starting_balance"]))
    embed = await hub_embed(bot, fake_user(), 2_500, settings)
    assert "2,500" in embed.description
    assert len(embed) <= 6_000  # Discord's total embed limit
    assert embed.footer.text


async def test_embeds_stay_inside_discords_field_limits(bot):
    settings = await bot.guilds_store.settings(7)
    embed = await hub_embed(bot, fake_user(), 10**12, settings)
    assert len(embed.fields) <= 25
    for field in embed.fields:
        assert len(field.name) <= 256
        assert len(field.value) <= 1_024


@pytest.mark.parametrize(
    "amount,expected",
    [(0, "0"), (999, "999"), (1_500, "1.5K"), (2_000_000, "2M"), (-3_400, "-3.4K")],
)
def test_compact_numbers_read_the_way_people_write_them(amount, expected):
    assert theme.compact(amount) == expected


def test_progress_bars_never_overflow_their_width():
    for current, total in ((0, 10), (5, 10), (10, 10), (99, 10), (-1, 10), (1, 0)):
        bar = theme.progress_bar(current, total, width=12)
        assert len(bar) == 12


def test_money_formatting_is_consistent():
    assert theme.chips(1234567) == "1,234,567 🪙"
    assert theme.signed(-50) == "-50"
    assert theme.signed(50) == "+50"
    assert theme.multiplier(2.0) == "x2"
    assert theme.multiplier(1.985) == "x1.99"
