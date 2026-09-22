"""Profiles, levels, challenges, the cosmetic shop, and responsible play.

The profile card is the screen players look at most, so it gets the most
care: a level bar, a win rate, the game you actually play most (counted, not
guessed from the last round), and your equipped title.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from casino.core.errors import CasinoError
from casino.db.economy import AlreadyClaimed, period_keys
from casino.db.progression import (
    ACHIEVEMENTS,
    BADGE_EMOJI,
    SHOP,
    SHOP_BY_ID,
    xp_for_level,
)
from casino.games.rng import verify as verify_commitment
from casino.ui import theme
from casino.ui.responder import Responder
from casino.ui.views import CasinoView

# (name, description, counter, target, reward)
DAILY_CHALLENGES = (
    ("Grinder", "Play 10 rounds", "games", 10, 1_500),
    ("High roller", "Stake 25,000 chips", "wagered", 25_000, 2_000),
    ("Sharp", "Win 5 rounds", "wins", 5, 1_800),
    ("Clocking in", "Work 4 shifts", "work", 4, 1_400),
)

WEEKLY_CHALLENGES = (
    ("Fixture", "Play 100 rounds", "games", 100, 12_000),
    ("Whale watch", "Stake 500,000 chips", "wagered", 500_000, 20_000),
    ("Streak hunter", "Win 40 rounds", "wins", 40, 18_000),
    ("Full timer", "Work 30 shifts", "work", 30, 10_000),
)


def pick_challenge(pool: tuple[Any, ...], seed: str) -> tuple[Any, ...]:
    """Deterministically choose the challenge for a period.

    Everyone in every server gets the same challenge for a given day, and it
    can be recomputed at any time from the date alone — no extra storage.
    """
    return pool[sum(ord(char) for char in seed) % len(pool)]


class Progression(commands.Cog, name="Progression"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # profile
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="profile", aliases=["stats", "me"], description="Show a player's casino profile.")
    @app_commands.describe(member="Whose profile to show.")
    async def profile_command(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        await self._profile(Responder(ctx), member or ctx.author)

    async def show_profile(self, interaction: discord.Interaction, member: discord.abc.User) -> None:
        await self._profile(Responder(interaction), member, ephemeral=True)

    async def _profile(self, responder: Responder, member: discord.abc.User, *, ephemeral: bool = False) -> None:
        guild_id = responder.guild_id
        await self.bot.guard.ensure_player(guild_id, member.id)
        profile = await self.bot.economy.profile(guild_id, member.id)
        state = await self.bot.progression.get(guild_id, member.id)
        unlocked = await self.bot.progression.unlocked(guild_id, member.id)
        breakdown = await self.bot.economy.game_breakdown(guild_id, member.id)

        games = int(profile.get("games", 0))
        wins = int(profile.get("wins", 0))
        decided = wins + int(profile.get("losses", 0))
        win_rate = theme.percent(wins / decided) if decided else "—"
        needed = xp_for_level(state["level"])
        title = SHOP_BY_ID[state["title"]].name if state.get("title") in SHOP_BY_ID else None
        badge = BADGE_EMOJI.get(state.get("badge") or "", "")

        header = f"{badge} **{title}**\n" if title else ""
        embed = theme.embed(
            title=f"📇 {member.display_name}",
            description=(
                f"{header}"
                f"{theme.ICON['level']} **Level {state['level']}**"
                + (f" {theme.ICON['prestige']}{state['prestige']}" if state["prestige"] else "")
                + f"\n`{theme.progress_bar(state['xp'], needed, 16)}` {state['xp']:,}/{needed:,} XP"
            ),
            color=theme.BRAND,
            thumbnail=getattr(getattr(member, "display_avatar", None), "url", None),
        )
        embed.add_field(name="Balance", value=theme.chips(profile.get("balance", 0)), inline=True)
        embed.add_field(name="Net profit", value=theme.signed(profile.get("net", 0)), inline=True)
        embed.add_field(name="Total staked", value=f"{int(profile.get('wagered', 0)):,}", inline=True)
        embed.add_field(name="Rounds", value=f"{games:,}", inline=True)
        embed.add_field(name="Win rate", value=f"{win_rate} ({wins:,} won)", inline=True)
        embed.add_field(
            name="Streak",
            value=f"{int(profile.get('streak', 0))} now · {int(profile.get('best_streak', 0))} best",
            inline=True,
        )
        embed.add_field(name="Biggest win", value=theme.chips(profile.get("biggest_win", 0)), inline=True)
        embed.add_field(name="Biggest loss", value=theme.chips(profile.get("biggest_loss", 0)), inline=True)
        embed.add_field(
            name="Achievements", value=f"{len(unlocked)}/{len(ACHIEVEMENTS)}", inline=True
        )

        if breakdown:
            top = breakdown[:3]
            embed.add_field(
                name="Most played",
                value="\n".join(
                    f"**{row['game'].title()}** — {int(row['plays']):,} rounds, "
                    f"{theme.signed(row['net'])} net"
                    for row in top
                ),
                inline=False,
            )
        embed.set_footer(text="/achievements for the full list · /shop to spend chips on cosmetics")
        await responder.send(embed=embed, ephemeral=ephemeral)

    # ------------------------------------------------------------------
    # achievements
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="achievements", aliases=["ach"], description="Your achievement progress.")
    async def achievements_command(self, ctx: commands.Context) -> None:
        responder = Responder(ctx)
        guild_id = responder.guild_id
        await self.bot.guard.ensure_player(guild_id, responder.user.id)
        unlocked = await self.bot.progression.unlocked(guild_id, responder.user.id)
        lines = [
            f"{'✅' if item.id in unlocked else '🔒'} {item.icon} **{item.name}** — {item.description}"
            for item in ACHIEVEMENTS
        ]
        await responder.send(
            embed=theme.embed(
                title=f"{theme.ICON['trophy']} Achievements",
                description="\n".join(lines),
                color=theme.GOLD,
                author=responder.user,
                footer=f"{len(unlocked)}/{len(ACHIEVEMENTS)} unlocked",
            )
        )

    # ------------------------------------------------------------------
    # challenges
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="challenges", aliases=["quests"], description="Daily and weekly challenges.")
    async def challenges_command(self, ctx: commands.Context) -> None:
        await self._challenges(Responder(ctx))

    async def show_challenges(self, interaction: discord.Interaction) -> None:
        await self._challenges(Responder(interaction), ephemeral=True)

    async def _challenges(self, responder: Responder, *, ephemeral: bool = False) -> None:
        guild_id = responder.guild_id
        await self.bot.guard.ensure_player(guild_id, responder.user.id)
        embed, view = await self.build_challenges(responder.guild_id, responder.user)
        await responder.send(embed=embed, view=view, ephemeral=ephemeral)

    async def build_challenges(
        self, guild_id: int | None, user: discord.abc.User
    ) -> tuple[discord.Embed, "ChallengeView"]:
        keys = period_keys()
        daily = pick_challenge(DAILY_CHALLENGES, keys["daily"])
        weekly = pick_challenge(WEEKLY_CHALLENGES, keys["weekly"])
        daily_progress = await self.bot.economy.challenge(guild_id, user.id, "daily")
        weekly_progress = await self.bot.economy.challenge(guild_id, user.id, "weekly")

        def block(challenge: tuple[Any, ...], progress: dict[str, Any], window: str) -> str:
            name, description, counter, target, reward = challenge
            current = min(int(progress.get(counter, 0)), target)
            done = current >= target
            claimed = bool(progress.get("claimed"))
            status = "✅ claimed" if claimed else "🎁 ready to claim" if done else f"{window}"
            return (
                f"**{name}** — {description}\n"
                f"`{theme.progress_bar(current, target, 14)}` {current:,}/{target:,}\n"
                f"Reward **{reward:,}** chips · {status}"
            )

        now = datetime.now(timezone.utc)
        midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        week_end = midnight + timedelta(days=6 - now.weekday())

        embed = theme.embed(
            title="🎯 Challenges",
            color=theme.GOLD,
            author=user,
            footer="Progress counts every round you play, including losses.",
        )
        embed.add_field(
            name=f"Daily · resets {theme.relative(midnight)}",
            value=block(daily, daily_progress, f"resets {theme.relative(midnight)}"),
            inline=False,
        )
        embed.add_field(
            name=f"Weekly · resets {theme.relative(week_end)}",
            value=block(weekly, weekly_progress, f"resets {theme.relative(week_end)}"),
            inline=False,
        )
        view = ChallengeView(self.bot, user, daily, weekly, daily_progress, weekly_progress)
        return embed, view

    # ------------------------------------------------------------------
    # shop
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="shop", description="Spend chips on titles and profile trim.")
    async def shop_command(self, ctx: commands.Context) -> None:
        await self._shop(Responder(ctx))

    async def show_shop(self, interaction: discord.Interaction) -> None:
        await self._shop(Responder(interaction), ephemeral=True)

    async def _shop(self, responder: Responder, *, ephemeral: bool = False) -> None:
        guild_id = responder.guild_id
        await self.bot.guard.ensure_player(guild_id, responder.user.id)
        owned = await self.bot.progression.inventory(guild_id, responder.user.id)
        state = await self.bot.progression.get(guild_id, responder.user.id)
        balance = await self.bot.economy.balance(guild_id, responder.user.id)

        lines = []
        for item in SHOP:
            mark = "✅" if item.id in owned else f"{item.price:,} chips"
            gate = f" · needs level {item.level_required}" if item.level_required else ""
            equipped = " · **equipped**" if state.get("title") == item.id or state.get("badge") == item.id else ""
            lines.append(f"{BADGE_EMOJI.get(item.id, '🏷️')} **{item.name}** — {item.description}\n　{mark}{gate}{equipped}")

        embed = theme.embed(
            title=f"{theme.ICON['shop']} Cosmetics shop",
            description="\n".join(lines),
            color=theme.BRAND,
            author=responder.user,
            footer=f"You have {balance:,} chips · buying an item equips it",
        )
        view = ShopView(self.bot, responder.user, owned, state, balance)
        await responder.send(embed=embed, view=view, ephemeral=ephemeral)

    # ------------------------------------------------------------------
    # prestige
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="prestige", description="Trade a maxed level for a prestige star.")
    async def prestige(self, ctx: commands.Context) -> None:
        responder = Responder(ctx)
        guild_id = responder.guild_id
        state = await self.bot.progression.get(guild_id, responder.user.id)
        if state["level"] < 50:
            raise CasinoError(
                f"Prestige unlocks at level 50. You are level **{state['level']}**."
            )
        value = await self.bot.progression.prestige(guild_id, responder.user.id)
        await responder.send(
            embed=theme.embed(
                title=f"{theme.ICON['prestige']} Prestige {value}",
                description="Your level resets to 1 and a star is added to your profile permanently.",
                color=theme.GOLD,
                author=responder.user,
            )
        )

    # ------------------------------------------------------------------
    # responsible play
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="wagerlimit", description="Cap how much you can stake per day.")
    @app_commands.describe(amount="A daily cap in chips, or 'off' to remove it.")
    async def wagerlimit(self, ctx: commands.Context, amount: str) -> None:
        responder = Responder(ctx)
        guild_id = responder.guild_id
        if amount.strip().lower() in ("off", "none", "0"):
            await self.bot.progression.set_wager_limit(guild_id, responder.user.id, None)
            return await responder.send(
                embed=theme.success("Your daily wager limit has been removed."), ephemeral=True
            )
        limit = await self.bot.guard.resolve_stake(guild_id, responder.user.id, amount, minimum=1)
        await self.bot.progression.set_wager_limit(guild_id, responder.user.id, limit)
        await responder.send(
            embed=theme.success(
                f"You will not be able to stake more than **{limit:,}** chips in a day. "
                "It resets at midnight UTC."
            ),
            ephemeral=True,
        )

    @commands.hybrid_command(name="selfexclude", description="Lock yourself out of the casino for a while.")
    @app_commands.describe(days="How many days to stay out. This cannot be undone early.")
    async def selfexclude(self, ctx: commands.Context, days: int) -> None:
        responder = Responder(ctx)
        if not 1 <= days <= 365:
            raise CasinoError("Choose between 1 and 365 days.")
        view = CasinoView(owner_id=responder.user.id, timeout=60)

        async def confirm(interaction: discord.Interaction) -> None:
            until = await self.bot.progression.set_self_exclusion(
                responder.guild_id, responder.user.id, timedelta(days=days)
            )
            await interaction.response.edit_message(
                embed=theme.embed(
                    title="🛑 Self-exclusion active",
                    description=(
                        f"You are locked out of every game until "
                        f"{discord.utils.format_dt(until, 'F')}.\n\n"
                        "Nobody can shorten this, including staff and a full account "
                        "reset. Your chips and stats are untouched and will be waiting."
                    ),
                    color=theme.DANGER,
                ),
                view=None,
            )

        button: discord.ui.Button = discord.ui.Button(
            label=f"Lock me out for {days} day(s)", style=discord.ButtonStyle.danger, emoji="🛑"
        )
        button.callback = confirm  # type: ignore[method-assign]
        view.add_item(button)

        await responder.send(
            embed=theme.embed(
                title="🛑 Confirm self-exclusion",
                description=(
                    f"This blocks **every game** for **{days} day(s)** and **cannot be lifted "
                    "early** by you, by staff, or by resetting your account.\n\n"
                    "`/balance`, `/profile` and the leaderboards keep working."
                ),
                color=theme.WARN,
            ),
            view=view,
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # fairness
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="verify", description="Check a revealed round seed against its published hash.")
    @app_commands.describe(
        server_seed="The seed revealed after the round.",
        commitment="The hash shown in the round's footer.",
    )
    async def verify(self, ctx: commands.Context, server_seed: str, commitment: str) -> None:
        from casino.games.rng import commitment as hash_seed

        digest = hash_seed(server_seed.strip())
        supplied = commitment.strip().lower()
        matches = digest.startswith(supplied) if len(supplied) < 64 else verify_commitment(server_seed, supplied)

        await Responder(ctx).send(
            embed=theme.embed(
                title=f"{theme.ICON['fair']} {'Seed verified' if matches else 'Seed does not match'}",
                description=(
                    f"**SHA-256 of the seed**\n`{digest}`\n\n"
                    f"**You supplied**\n`{supplied}`\n\n"
                    + (
                        "The hash matches, so this really was the seed committed to before the round."
                        if matches
                        else "These do not match. Either the seed or the hash was mistyped."
                    )
                ),
                color=theme.WIN if matches else theme.LOSE,
            ),
            ephemeral=True,
        )


class ChallengeView(CasinoView):
    """Claim buttons that can each only ever pay out once."""

    def __init__(
        self,
        bot: Any,
        user: discord.abc.User,
        daily: tuple[Any, ...],
        weekly: tuple[Any, ...],
        daily_progress: dict[str, Any],
        weekly_progress: dict[str, Any],
    ) -> None:
        super().__init__(owner_id=user.id, timeout=180)
        self.bot = bot
        self.user = user
        self.challenges = {"daily": daily, "weekly": weekly}
        self.progress = {"daily": daily_progress, "weekly": weekly_progress}
        for kind, button in (("daily", self.claim_daily), ("weekly", self.claim_weekly)):
            challenge = self.challenges[kind]
            progress = self.progress[kind]
            button.disabled = bool(progress.get("claimed")) or int(progress.get(challenge[2], 0)) < challenge[3]

    async def _claim(self, interaction: discord.Interaction, kind: str) -> None:
        name, _, counter, target, reward = self.challenges[kind]
        progress = await self.bot.economy.challenge(interaction.guild_id, self.user.id, kind)
        if int(progress.get(counter, 0)) < target:
            raise CasinoError(
                f"You need **{target - int(progress.get(counter, 0)):,}** more before that pays out."
            )
        try:
            balance = await self.bot.economy.claim_challenge(
                interaction.guild_id, self.user.id, kind, reward, name
            )
        except AlreadyClaimed:
            raise CasinoError("That reward has already been claimed this period.") from None
        self.disable_all()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            embed=theme.success(
                f"**{name}** complete — **+{reward:,}** chips.\n"
                f"{theme.ICON['balance']} Balance **{balance:,}**",
                title=f"{theme.ICON['trophy']} {kind.title()} challenge",
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Claim daily", emoji="🎁", style=discord.ButtonStyle.success)
    async def claim_daily(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._claim(interaction, "daily")

    @discord.ui.button(label="Claim weekly", emoji="🏆", style=discord.ButtonStyle.success)
    async def claim_weekly(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._claim(interaction, "weekly")


class ShopView(CasinoView):
    """Buy or equip in one click, with everything you cannot afford greyed out."""

    def __init__(
        self,
        bot: Any,
        user: discord.abc.User,
        owned: set[str],
        state: dict[str, Any],
        balance: int,
    ) -> None:
        super().__init__(owner_id=user.id, timeout=180)
        self.bot = bot
        self.user = user
        self.owned = owned
        self.state = state
        options = []
        for item in SHOP:
            if item.id in owned:
                description = "Owned — select to equip"
            elif balance < item.price:
                description = f"{item.price:,} chips — you need {item.price - balance:,} more"
            else:
                description = f"{item.price:,} chips"
            options.append(
                discord.SelectOption(
                    label=item.name,
                    value=item.id,
                    description=description[:95],
                    emoji=BADGE_EMOJI.get(item.id, "🏷️"),
                )
            )
        select: discord.ui.Select = discord.ui.Select(placeholder="Buy or equip…", options=options)
        select.callback = self._select  # type: ignore[method-assign]
        self.select = select
        self.add_item(select)

    async def _select(self, interaction: discord.Interaction) -> None:
        item = SHOP_BY_ID[self.select.values[0]]
        guild_id = interaction.guild_id
        state = await self.bot.progression.get(guild_id, self.user.id)

        if item.id not in self.owned:
            if item.level_required and state["level"] < item.level_required:
                raise CasinoError(
                    f"**{item.name}** unlocks at level {item.level_required}; you are level {state['level']}."
                )
            # adjust() refuses to go negative, so this is the price check too.
            await self.bot.economy.adjust(
                guild_id, self.user.id, -item.price, kind="shop", note=item.name
            )
            await self.bot.progression.grant(guild_id, self.user.id, item.id)
            self.owned.add(item.id)
            verb = "Bought and equipped"
        else:
            verb = "Equipped"
        await self.bot.progression.equip(guild_id, self.user.id, item)
        balance = await self.bot.economy.balance(guild_id, self.user.id)
        await interaction.response.send_message(
            embed=theme.success(
                f"{verb} **{item.name}**.\n{theme.ICON['balance']} Balance **{balance:,}**"
            ),
            ephemeral=True,
        )


async def setup(bot: Any) -> None:
    await bot.add_cog(Progression(bot))
