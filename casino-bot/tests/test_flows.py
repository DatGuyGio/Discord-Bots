"""Playing actual rounds through the actual command handlers.

Unit tests cover the maths and the ledger; this file drives the Discord-facing
code — the cogs, the responder, the views — with stand-in Interaction and
Context objects. That catches the class of bug no amount of logic testing
finds: an embed field built from a renamed attribute, a view whose callback
never fires, a flow that debits a stake and then throws before settling it.

The old single-file bot had eleven hand-written copies of "take the bet,
resolve it, pay out, post an embed", and the only way to exercise any of them
was to open Discord and place a bet.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio
from discord.ext import commands

from casino.config import Settings
from casino.core.bot import CasinoBot
from casino.core.errors import BetOutOfRange, CasinoError, GameDisabled
from casino.games import mines as mines_game
from casino.ui.games import MinesView

GUILD = 31337
PLAYER = 4242


@pytest_asyncio.fixture
async def bot(tmp_path, monkeypatch):
    # Animations are cosmetic; skipping the waits keeps the suite fast.
    async def instant(_seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", instant)

    instance = CasinoBot(Settings(token="dummy", database_path=str(tmp_path / "flows.sqlite3")))
    await instance.prepare()
    await instance.guilds_store.update(GUILD, min_bet=10, max_bet=10_000, starting_balance=50_000)
    await instance.economy.ensure(GUILD, PLAYER, 50_000)
    yield instance
    await instance.db.close()


def fake_guild() -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.name = "Test Server"
    guild.get_channel.return_value = None
    guild.get_member.side_effect = lambda user_id: fake_user(user_id)
    return guild


def fake_user(user_id: int = PLAYER) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        display_name=f"Player{user_id}",
        mention=f"<@{user_id}>",
        bot=False,
        display_avatar=SimpleNamespace(url="https://example.invalid/avatar.png"),
    )


def fake_message() -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock(return_value=message)
    message.delete = AsyncMock()
    message.guild = fake_guild()
    return message


def fake_context(bot: CasinoBot) -> MagicMock:
    """A prefix-command invocation."""
    ctx = MagicMock(spec=commands.Context)
    ctx.bot = bot
    ctx.author = fake_user()
    ctx.guild = fake_guild()
    ctx.interaction = None
    ctx.channel = MagicMock(spec=discord.TextChannel)
    ctx.send = AsyncMock(return_value=fake_message())
    return ctx


def fake_interaction(bot: CasinoBot, *, done: bool = False) -> MagicMock:
    """A button, select or modal submission."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.user = fake_user()
    interaction.guild = fake_guild()
    interaction.guild_id = GUILD
    interaction.message = fake_message()
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = done
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock(return_value=fake_message())
    interaction.original_response = AsyncMock(return_value=fake_message())
    return interaction


def sent_embeds(mock: AsyncMock) -> list[discord.Embed]:
    return [call.kwargs["embed"] for call in mock.call_args_list if call.kwargs.get("embed")]


def last_embed(*mocks: AsyncMock) -> discord.Embed:
    embeds = [embed for mock in mocks for embed in sent_embeds(mock)]
    assert embeds, "no embed was ever sent"
    return embeds[-1]


def is_renderable(embed: discord.Embed) -> bool:
    """Everything Discord will reject at send time, checked locally."""
    assert len(embed) <= 6_000
    assert embed.title is None or len(embed.title) <= 256
    assert embed.description is None or len(embed.description) <= 4_096
    assert len(embed.fields) <= 25
    for field in embed.fields:
        assert 0 < len(field.name) <= 256
        assert 0 < len(field.value) <= 1_024
    return True


# ---------------------------------------------------------------------------
# slots
# ---------------------------------------------------------------------------


async def test_a_slots_round_debits_settles_and_reports(bot):
    games = bot.get_cog("Games")
    ctx = fake_context(bot)
    before = await bot.economy.balance(GUILD, PLAYER)

    await games.play_slots(ctx, "500")

    profile = await bot.economy.profile(GUILD, PLAYER)
    assert profile["games"] == 1
    assert profile["wagered"] == 500
    # Either the stake went or a win came back, but never both and never neither.
    assert profile["balance"] in range(before - 500, before + 500 * 1_500 + 1)
    embed = last_embed(ctx.send, *[m.edit for m in [ctx.send.return_value]])
    assert is_renderable(embed)


async def test_the_slots_animation_edits_one_message_rather_than_spamming(bot):
    games = bot.get_cog("Games")
    ctx = fake_context(bot)
    message = ctx.send.return_value

    await games.play_slots(ctx, "100")

    assert ctx.send.await_count == 1
    assert message.edit.await_count >= 6  # frames plus the final result


async def test_a_button_bet_is_held_to_the_same_limits_as_a_typed_one(bot):
    """The bug class this closes: button and modal flows used to skip the
    maximum-bet check entirely, which is how a 10^27 chip bet was accepted."""
    games = bot.get_cog("Games")

    with pytest.raises(BetOutOfRange):
        await games.play_slots(fake_interaction(bot), "20000")  # above max_bet
    with pytest.raises(BetOutOfRange):
        await games.play_slots(fake_context(bot), "20000")  # same answer, typed

    assert (await bot.economy.profile(GUILD, PLAYER))["games"] == 0
    assert await bot.economy.balance(GUILD, PLAYER) == 50_000


async def test_slots_result_carries_replay_controls(bot):
    games = bot.get_cog("Games")
    ctx = fake_context(bot)
    message = ctx.send.return_value

    await games.play_slots(ctx, "250")

    views = [call.kwargs.get("view") for call in message.edit.call_args_list if call.kwargs.get("view")]
    assert views, "no replay view was attached"
    labels = [child.label for child in views[-1].children]
    assert any("250" in (label or "") for label in labels)
    assert any("500" in (label or "") for label in labels)  # the double button


# ---------------------------------------------------------------------------
# roulette
# ---------------------------------------------------------------------------


async def test_a_roulette_round_resolves_a_named_bet(bot):
    games = bot.get_cog("Games")
    ctx = fake_context(bot)

    await games.play_roulette(ctx, "300", "black")

    profile = await bot.economy.profile(GUILD, PLAYER)
    assert profile["games"] == 1
    assert profile["last_bet_detail"] == "black"
    assert await bot.economy.favourite_game(GUILD, PLAYER) == "roulette"


async def test_roulette_rejects_a_bet_that_is_not_on_the_table(bot):
    games = bot.get_cog("Games")
    ctx = fake_context(bot)
    with pytest.raises(CasinoError):
        await games.play_roulette(ctx, "300", "purple")
    assert await bot.economy.balance(GUILD, PLAYER) == 50_000


async def test_roulette_with_no_arguments_opens_the_bet_board(bot):
    games = bot.get_cog("Games")
    ctx = fake_context(bot)
    ctx.command = MagicMock(qualified_name="roulette")

    await games.roulette_command(ctx)

    view = ctx.send.call_args.kwargs["view"]
    labels = {child.label for child in view.children if getattr(child, "label", None)}
    assert {"Red", "Black", "Odd", "Even"} <= labels
    assert (await bot.economy.profile(GUILD, PLAYER))["games"] == 0  # nothing staked yet


# ---------------------------------------------------------------------------
# dice and coinflip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", [2, 4, 6])
async def test_dice_pays_the_odds_of_the_target_chosen(bot, target):
    games = bot.get_cog("Games")
    ctx = fake_context(bot)

    await games.play_dice(ctx, "100", target)

    embed = last_embed(ctx.send)
    assert is_renderable(embed)
    chance = (7 - target) / 6
    assert f"{chance * 100:.1f}%" in embed.description


@pytest.mark.parametrize("target", [0, 1, 7, "x"])
async def test_dice_refuses_an_impossible_target(bot, target):
    games = bot.get_cog("Games")
    with pytest.raises(CasinoError):
        await games.play_dice(fake_context(bot), "100", target)


async def test_coinflip_accepts_shorthand_sides(bot):
    games = bot.get_cog("Games")
    for side in ("h", "heads", "T", "tails"):
        await games.play_coinflip(fake_context(bot), "50", side)
    assert (await bot.economy.profile(GUILD, PLAYER))["games"] == 4


async def test_coinflip_refuses_a_side_that_does_not_exist(bot):
    games = bot.get_cog("Games")
    with pytest.raises(CasinoError):
        await games.play_coinflip(fake_context(bot), "50", "edge")


# ---------------------------------------------------------------------------
# mines
# ---------------------------------------------------------------------------


async def test_opening_mines_posts_a_full_grid(bot):
    games = bot.get_cog("Games")
    ctx = fake_context(bot)

    await games.play_mines(ctx, "500", 3)

    view = ctx.send.call_args.kwargs["view"]
    assert isinstance(view, MinesView)
    tiles = [child for child in view.children if getattr(child, "index", None) is not None]
    assert len(tiles) == mines_game.TILES
    assert await bot.economy.balance(GUILD, PLAYER) == 49_500  # stake already held


async def test_revealing_a_safe_tile_raises_the_multiplier(bot):
    view = await open_mines(bot, stake=500, mines=3)
    safe = next(i for i in range(mines_game.TILES) if i not in view.board.mine_positions)
    interaction = fake_interaction(bot)

    await view.click(interaction, safe)

    assert view.board.picks == 1
    assert view.tiles[safe].disabled
    assert view.tiles[safe].label == "💎"
    embed = interaction.response.edit_message.call_args.kwargs["embed"]
    assert is_renderable(embed)
    assert not view.board.finished  # still live, nothing settled


async def test_cashing_out_pays_the_ladder_and_ends_the_round(bot):
    view = await open_mines(bot, stake=500, mines=3)
    safe = [i for i in range(mines_game.TILES) if i not in view.board.mine_positions]
    for tile in safe[:3]:
        await view.click(fake_interaction(bot), tile)

    expected = int(500 * mines_game.multiplier(3, 3, view.board.rtp))
    interaction = fake_interaction(bot)
    await view.cash_out_button.callback(interaction)

    assert view.board.cashed_out
    assert await bot.economy.balance(GUILD, PLAYER) == 49_500 + expected
    profile = await bot.economy.profile(GUILD, PLAYER)
    assert profile["games"] == 1
    assert profile["net"] == expected - 500


async def test_hitting_a_mine_loses_the_stake_and_reveals_the_board(bot):
    view = await open_mines(bot, stake=500, mines=3)
    bomb = next(iter(view.board.mine_positions))
    interaction = fake_interaction(bot)

    await view.click(interaction, bomb)

    assert view.board.exploded_at == bomb
    assert view.tiles[bomb].label == "💥"
    assert all(child.disabled for child in view.children)
    assert await bot.economy.balance(GUILD, PLAYER) == 49_500
    assert (await bot.economy.profile(GUILD, PLAYER))["net"] == -500


async def test_cashing_out_before_any_click_returns_the_stake(bot):
    """Not a zero-multiplier win: the round never really started."""
    view = await open_mines(bot, stake=500, mines=3)
    await view.cash_out_button.callback(fake_interaction(bot))

    assert await bot.economy.balance(GUILD, PLAYER) == 50_000
    assert (await bot.economy.profile(GUILD, PLAYER))["games"] == 0


async def test_a_finished_round_refuses_further_clicks(bot):
    """Two fast clicks must not settle the same round twice."""
    from casino.core.errors import RoundFinished

    view = await open_mines(bot, stake=500, mines=3)
    bomb = next(iter(view.board.mine_positions))
    await view.click(fake_interaction(bot), bomb)
    safe = next(i for i in range(mines_game.TILES) if i not in view.board.mine_positions)
    with pytest.raises(RoundFinished):
        await view.click(fake_interaction(bot), safe)


async def test_mines_refuses_an_impossible_bomb_count(bot):
    games = bot.get_cog("Games")
    for count in (0, mines_game.TILES, 99):
        with pytest.raises(CasinoError):
            await games.play_mines(fake_context(bot), "100", count)


async def open_mines(bot: CasinoBot, *, stake: int, mines: int) -> MinesView:
    ctx = fake_context(bot)
    await bot.get_cog("Games").play_mines(ctx, str(stake), mines)
    return ctx.send.call_args.kwargs["view"]


# ---------------------------------------------------------------------------
# blackjack
# ---------------------------------------------------------------------------


async def deal_hand(bot: CasinoBot, stake: int = 1_000):
    ctx = fake_context(bot)
    await bot.get_cog("Blackjack").play_hand(ctx, str(stake))
    view = ctx.send.call_args.kwargs.get("view")
    return ctx, view


async def test_a_hand_is_dealt_into_one_message_with_controls(bot):
    ctx, table = await deal_hand(bot)
    if table is None:
        # A natural on the first two cards settles immediately and correctly.
        profile = await bot.economy.profile(GUILD, PLAYER)
        assert profile["games"] == 1
        return
    assert len(table.hands) == 1
    assert len(table.hands[0].logical.cards) == 2
    assert len(table.dealer) == 2
    labels = {child.label for child in table.children}
    assert {"Hit", "Stand", "Double", "Split", "Insurance"} <= labels
    assert await bot.economy.balance(GUILD, PLAYER) == 49_000


async def test_the_dealer_hole_card_is_hidden_until_the_hand_ends(bot):
    ctx, table = await deal_hand(bot)
    if table is None:
        return
    hidden = table.embed()
    revealed = table.embed(reveal=True)
    assert "🂠" in hidden.description
    assert "🂠" not in revealed.description
    assert is_renderable(hidden) and is_renderable(revealed)


async def test_standing_settles_the_hand_exactly_once(bot):
    ctx, table = await deal_hand(bot)
    if table is None:
        return
    interaction = fake_interaction(bot)

    await table.stand.callback(interaction)

    assert table.finished
    profile = await bot.economy.profile(GUILD, PLAYER)
    assert profile["games"] == 1
    assert profile["wagered"] == 1_000
    # Settled through the message, and the controls are gone.
    view = table.message.edit.call_args.kwargs["view"]
    assert all(child.disabled for child in table.children)
    assert view is not table


async def test_hitting_until_bust_loses_the_stake(bot):
    ctx, table = await deal_hand(bot)
    if table is None:
        return
    for _ in range(10):
        if table.finished:
            break
        await table.hit.callback(fake_interaction(bot))

    assert table.finished
    profile = await bot.economy.profile(GUILD, PLAYER)
    assert profile["games"] >= 1
    assert await bot.economy.balance(GUILD, PLAYER) <= 50_000


async def test_doubling_takes_a_second_stake_and_one_card(bot):
    for _ in range(12):  # keep dealing until a doubleable hand shows up
        ctx, table = await deal_hand(bot)
        if table is None:
            continue
        if table.seat.current.can_double:
            break
    else:
        pytest.skip("no doubleable hand dealt")

    before = await bot.economy.balance(GUILD, PLAYER)
    await table.double.callback(fake_interaction(bot))

    assert table.finished
    assert table.hands[0].logical.bet == 2_000
    assert table.hands[0].logical.doubled
    assert len(table.hands[0].logical.cards) == 3
    profile = await bot.economy.profile(GUILD, PLAYER)
    # One round in the history, staked at the doubled amount.
    assert profile["last_stake"] == 2_000
    assert before - 1_000 <= profile["balance"] <= before + 3_000


async def test_an_abandoned_hand_settles_itself_on_timeout(bot):
    """Otherwise the stake stays debited and the chips simply vanish."""
    ctx, table = await deal_hand(bot)
    if table is None:
        return
    await table.on_timeout()

    assert table.finished
    profile = await bot.economy.profile(GUILD, PLAYER)
    assert profile["games"] == 1


async def test_a_settled_table_ignores_further_button_presses(bot):
    from casino.core.errors import RoundFinished

    ctx, table = await deal_hand(bot)
    if table is None:
        return
    await table.stand.callback(fake_interaction(bot))
    with pytest.raises(RoundFinished):
        await table.hit.callback(fake_interaction(bot))
    assert (await bot.economy.profile(GUILD, PLAYER))["games"] == 1


# ---------------------------------------------------------------------------
# hub and other surfaces
# ---------------------------------------------------------------------------


async def test_the_hub_opens_with_a_live_balance(bot):
    ctx = fake_context(bot)
    await bot.get_cog("Hub").casino(ctx)
    embed = ctx.send.call_args.kwargs["embed"]
    assert "50,000" in embed.description
    assert is_renderable(embed)


async def test_a_hub_game_button_opens_a_stake_prompt(bot):
    from casino.ui.hub import HubView

    view = HubView(bot)
    interaction = fake_interaction(bot)
    await view.slots.callback(interaction)

    modal = interaction.response.send_modal.call_args.args[0]
    assert "Slots" in modal.title
    # Pre-filled with the table minimum for a player who has not bet yet.
    assert modal.amount.default == "10"


async def test_a_switched_off_game_is_refused_at_the_button(bot):
    from casino.ui.hub import HubView

    await bot.guilds_store.set_game(GUILD, "slots", False)
    view = HubView(bot)
    interaction = fake_interaction(bot)

    await view.slots.callback(interaction)

    interaction.response.send_modal.assert_not_awaited()
    embed = interaction.response.send_message.call_args.kwargs["embed"]
    assert "switched off" in embed.description
    with pytest.raises(GameDisabled):
        await bot.guard.check_game(GUILD, "slots")


async def test_the_hub_wallet_button_reaches_the_economy_cog(bot):
    from casino.ui.hub import HubView

    view = HubView(bot)
    interaction = fake_interaction(bot)
    await view.wallet.callback(interaction)
    embed = interaction.response.send_message.call_args.kwargs["embed"]
    assert "50,000" in embed.description
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_claiming_the_daily_from_the_hub_pays_once(bot):
    from casino.core.errors import OnCooldown
    from casino.ui.hub import HubView

    view = HubView(bot)
    await view.daily.callback(fake_interaction(bot))
    assert await bot.economy.balance(GUILD, PLAYER) > 50_000
    with pytest.raises(OnCooldown):
        await view.daily.callback(fake_interaction(bot))


async def test_the_leaderboard_renders_and_can_be_re_sliced(bot):
    boards = bot.get_cog("Leaderboards")
    ctx = fake_context(bot)
    await boards.leaderboard(ctx, "balance", "alltime")
    embed = ctx.send.call_args.kwargs["embed"]
    view = ctx.send.call_args.kwargs["view"]
    assert "Player4242" in embed.description
    assert is_renderable(embed)

    # Switching to a windowed metric and a recent period re-queries in place.
    interaction = fake_interaction(bot)
    view.metric_select._values = ["wagered"]
    await view._metric_changed(interaction)
    view = interaction.response.edit_message.call_args.kwargs["view"]
    view.window_select._values = ["weekly"]
    await view._window_changed(interaction)
    refreshed = interaction.response.edit_message.call_args.kwargs["embed"]
    assert "Last 7 days" in refreshed.title
    assert "Most staked" in refreshed.title


async def test_a_time_window_falls_back_for_metrics_it_cannot_answer(bot):
    """A balance is a snapshot, so "richest in the last 24 hours" is not a
    question the ledger can answer. The old bot returned an error; this
    quietly shows the all-time board instead."""
    boards = bot.get_cog("Leaderboards")
    embed, _ = await boards.build(fake_guild(), fake_user(), "balance", "weekly")
    assert "All time" in embed.title


async def test_the_profile_card_renders_for_a_player_with_history(bot):
    await bot.get_cog("Games").play_slots(fake_context(bot), "500")
    ctx = fake_context(bot)
    await bot.get_cog("Progression").profile_command(ctx, None)
    embed = ctx.send.call_args.kwargs["embed"]
    assert is_renderable(embed)
    assert any(field.name == "Win rate" for field in embed.fields)
    assert any(field.name == "Most played" for field in embed.fields)


async def test_challenges_render_with_progress_from_real_play(bot):
    games = bot.get_cog("Games")
    for _ in range(3):
        await games.play_slots(fake_context(bot), "100")
    ctx = fake_context(bot)
    await bot.get_cog("Progression").challenges_command(ctx)
    embed = ctx.send.call_args.kwargs["embed"]
    assert is_renderable(embed)
    assert "Daily" in embed.fields[0].name


async def test_the_shop_lists_items_and_marks_what_is_affordable(bot):
    ctx = fake_context(bot)
    await bot.get_cog("Progression").shop_command(ctx)
    embed = ctx.send.call_args.kwargs["embed"]
    view = ctx.send.call_args.kwargs["view"]
    assert is_renderable(embed)
    descriptions = [option.description for option in view.select.options]
    assert any("chips" in (text or "") for text in descriptions)


async def test_history_paginates_real_ledger_rows(bot):
    games = bot.get_cog("Games")
    for _ in range(10):
        await games.play_dice(fake_context(bot), "50", 4)
    ctx = fake_context(bot)
    await bot.get_cog("Economy").history(ctx)
    view = ctx.send.call_args.kwargs["view"]
    assert len(view.pages) >= 2
    for page in view.pages:
        assert is_renderable(page)


async def test_help_builds_a_page_for_every_category(bot):
    meta = bot.get_cog("Meta")
    ctx = fake_context(bot)
    await meta.help_command(ctx, None)
    view = ctx.send.call_args.kwargs["view"]
    assert len(view.select.options) > 5
    for option in view.select.options:
        if option.value == "__overview__":
            continue
        assert is_renderable(meta.category_embed(option.value, "!"))


async def test_analytics_summarises_the_house_position(bot):
    games = bot.get_cog("Games")
    for _ in range(5):
        await games.play_slots(fake_context(bot), "1000")
    ctx = fake_context(bot)
    await bot.get_cog("Admin").analytics(ctx)
    embed = ctx.send.call_args.kwargs["embed"]
    assert is_renderable(embed)
    assert any("Return to player" in field.name for field in embed.fields)


# ---------------------------------------------------------------------------
# crash
# ---------------------------------------------------------------------------


async def test_crash_pays_out_when_you_jump_off_in_time(bot, monkeypatch):
    from casino.games import crash as crash_game

    monkeypatch.setattr(crash_game, "TICK_SECONDS", 0.01)
    ctx = fake_context(bot)
    await bot.get_cog("Games").play_crash(ctx, "1000")
    view = ctx.send.call_args.kwargs["view"]
    view.task.cancel()  # drive the ticks by hand instead of by wall clock
    view.round.crash_at = 99.0  # a long ride, so the cash-out lands

    view.round.advance()
    view.round.advance()
    await view.cash_out.callback(fake_interaction(bot))

    expected = int(1_000 * view.round.cashed_at)
    assert await bot.economy.balance(GUILD, PLAYER) == 49_000 + expected
    assert (await bot.economy.profile(GUILD, PLAYER))["games"] == 1


async def test_a_crashed_ride_loses_the_stake_and_settles_itself(bot, monkeypatch):
    """The background ticker must settle the round even if nobody clicks."""
    from casino.games import crash as crash_game

    monkeypatch.setattr(crash_game, "TICK_SECONDS", 0.01)
    ctx = fake_context(bot)
    await bot.get_cog("Games").play_crash(ctx, "1000")
    view = ctx.send.call_args.kwargs["view"]
    view.task.cancel()
    view.round.crash_at = 1.00  # dies on the first tick

    await view.run()

    assert view.round.cashed_at is None
    assert await bot.economy.balance(GUILD, PLAYER) == 49_000
    assert (await bot.economy.profile(GUILD, PLAYER))["net"] == -1_000


async def test_cashing_out_after_a_crash_is_refused(bot, monkeypatch):
    from casino.core.errors import RoundFinished
    from casino.games import crash as crash_game

    monkeypatch.setattr(crash_game, "TICK_SECONDS", 0.01)
    ctx = fake_context(bot)
    await bot.get_cog("Games").play_crash(ctx, "1000")
    view = ctx.send.call_args.kwargs["view"]
    view.task.cancel()
    view.round.crash_at = 1.00
    await view.run()

    with pytest.raises(RoundFinished):
        await view.cash_out.callback(fake_interaction(bot))


# ---------------------------------------------------------------------------
# duels
# ---------------------------------------------------------------------------


async def test_a_duel_moves_chips_between_players_and_mints_none(bot):
    opponent_id = 5150
    await bot.economy.ensure(GUILD, opponent_id, 50_000)
    opponent = MagicMock(spec=discord.Member)
    opponent.id = opponent_id
    opponent.bot = False
    opponent.mention = f"<@{opponent_id}>"
    opponent.display_name = "Rival"
    opponent.display_avatar = SimpleNamespace(url="https://example.invalid/b.png")

    ctx = fake_context(bot)
    await bot.get_cog("Games").duel_command(ctx, opponent, "5000")
    view = ctx.send.call_args.kwargs["view"]

    interaction = fake_interaction(bot)
    interaction.user = fake_user(opponent_id)  # only the challenged player may accept
    await view.accept.callback(interaction)

    challenger_balance = await bot.economy.balance(GUILD, PLAYER)
    opponent_balance = await bot.economy.balance(GUILD, opponent_id)
    assert challenger_balance + opponent_balance == 100_000  # nothing created
    assert {challenger_balance, opponent_balance} == {45_000, 55_000}


async def test_declining_a_duel_moves_nothing(bot):
    opponent_id = 5151
    await bot.economy.ensure(GUILD, opponent_id, 50_000)
    opponent = MagicMock(spec=discord.Member)
    opponent.id = opponent_id
    opponent.bot = False
    opponent.mention = f"<@{opponent_id}>"
    opponent.display_name = "Rival"

    ctx = fake_context(bot)
    await bot.get_cog("Games").duel_command(ctx, opponent, "5000")
    view = ctx.send.call_args.kwargs["view"]

    interaction = fake_interaction(bot)
    interaction.user = fake_user(opponent_id)
    await view.decline.callback(interaction)

    assert await bot.economy.balance(GUILD, PLAYER) == 50_000
    assert await bot.economy.balance(GUILD, opponent_id) == 50_000


async def test_you_cannot_duel_a_player_who_cannot_cover_it(bot):
    opponent_id = 5152
    await bot.economy.ensure(GUILD, opponent_id, 10)
    opponent = MagicMock(spec=discord.Member)
    opponent.id = opponent_id
    opponent.bot = False
    opponent.mention = f"<@{opponent_id}>"
    opponent.display_name = "Skint"

    with pytest.raises(CasinoError):
        await bot.get_cog("Games").duel_command(fake_context(bot), opponent, "5000")
    assert await bot.economy.balance(GUILD, PLAYER) == 50_000
