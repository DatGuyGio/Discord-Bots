"""Wallet, faucets, jobs, transfers and history.

The chips have to come from somewhere, and the shape of that supply is what
makes an economy feel fair or broken. Two deliberate changes from the old
version:

* ``/work`` used to be available every three seconds at one point, then every
  30 minutes, with the payout read from module constants while the *timer*
  shown by ``!cooldowns`` was read from a different constant — so the screen
  and the reality disagreed. Both now come from the same guild setting.
* ``/daily`` had two different notions of when the next claim was due (a 24
  hour cooldown in one place, a UTC calendar day in another). It is now a
  calendar day everywhere, and the embed says exactly when midnight is.
"""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from casino.config import LEDGER_PAGE_SIZE
from casino.core.errors import CasinoError
from casino.db.economy import describe_upgrade, parse_iso
from casino.ui import theme
from casino.ui.responder import Responder
from casino.ui.views import Paginator

KIND_ICON = {
    "game": "🎲",
    "daily": "🎁",
    "work": "💼",
    "transfer": "🤝",
    "upgrade": "🧰",
    "shop": "🛍️",
    "challenge": "🎯",
    "refund": "↩️",
    "admin": "🛡️",
}


class Economy(commands.Cog, name="Economy"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # wallet
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="balance", aliases=["bal", "chips", "wallet"], description="Check your chips.")
    @app_commands.describe(member="Whose balance to look up.")
    async def balance_command(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        await self._wallet(Responder(ctx), member or ctx.author)

    async def show_wallet(self, interaction: discord.Interaction) -> None:
        await self._wallet(Responder(interaction), interaction.user, ephemeral=True)

    async def _wallet(self, responder: Responder, member: discord.abc.User, *, ephemeral: bool = False) -> None:
        guild_id = responder.guild_id
        await self.bot.guard.ensure_player(guild_id, member.id)
        profile = await self.bot.economy.profile(guild_id, member.id)
        state = await self.bot.progression.get(guild_id, member.id)
        settings = await self.bot.guilds_store.settings(guild_id)

        daily = await self.bot.guard.time_until_daily(guild_id, member.id)
        work = await self.bot.guard.time_until_work(guild_id, member.id)
        upgrade = describe_upgrade(settings, int(profile.get("work_level", 0)))

        embed = theme.embed(
            title=f"{theme.ICON['balance']} Wallet",
            description=f"# {int(profile['balance']):,} {theme.ICON['chips']}",
            color=theme.BRAND,
            author=member,
            thumbnail=getattr(getattr(member, "display_avatar", None), "url", None),
        )
        embed.add_field(
            name="Faucets",
            value=(
                f"{theme.ICON['daily']} **Daily** "
                + ("ready now" if daily is None else f"in {int(daily.total_seconds() // 3600)}h {int(daily.total_seconds() % 3600 // 60)}m")
                + f"\n{theme.ICON['work']} **Work** "
                + ("ready now" if work is None else f"in {int(work.total_seconds() // 60)}m {int(work.total_seconds() % 60)}s")
            ),
            inline=True,
        )
        embed.add_field(
            name="Job",
            value=(
                f"Level **{int(profile.get('work_level', 0))}** "
                f"(x{1 + float(settings['upgrade_multiplier_step']) * int(profile.get('work_level', 0)):g})\n"
                f"Next: **{upgrade['name']}** for {upgrade['cost']:,}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Streaks",
            value=(
                f"{theme.ICON['daily']} {int(profile.get('daily_streak', 0))} day(s)\n"
                f"{theme.ICON['streak']} {int(profile.get('streak', 0))} wins "
                f"(best {int(profile.get('best_streak', 0))})"
            ),
            inline=True,
        )
        embed.set_footer(text=f"Level {state['level']} · table limits {int(settings['min_bet']):,}–{int(settings['max_bet']):,}")
        await responder.send(embed=embed, ephemeral=ephemeral)

    # ------------------------------------------------------------------
    # faucets
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="daily", aliases=["claim"], description="Claim your daily chips.")
    async def daily_command(self, ctx: commands.Context) -> None:
        await self._daily(Responder(ctx))

    async def do_daily(self, interaction: discord.Interaction) -> None:
        await self._daily(Responder(interaction), ephemeral=True)

    async def _daily(self, responder: Responder, *, ephemeral: bool = False) -> None:
        guild_id = responder.guild_id
        settings = await self.bot.guard.ensure_player(guild_id, responder.user.id)
        amount, streak = await self.bot.economy.claim_daily(guild_id, responder.user.id, settings)
        balance = await self.bot.economy.balance(guild_id, responder.user.id)
        bonus = min(1.0 + max(0, streak - 1) * float(settings["daily_streak_bonus"]), 2.0)

        embed = theme.embed(
            title=f"{theme.ICON['daily']} Daily claimed",
            description=(
                f"**+{amount:,}** chips\n"
                f"{theme.ICON['streak']} **{streak} day streak** "
                f"(bonus {theme.multiplier(bonus)}, caps at x2)\n"
                f"{theme.ICON['balance']} Balance **{balance:,}**"
            ),
            color=theme.WIN,
            author=responder.user,
            footer="Resets at midnight UTC — claim on consecutive days to grow the bonus.",
        )
        await responder.send(embed=embed, ephemeral=ephemeral)

    @commands.hybrid_command(name="work", description="Work a shift for chips.")
    async def work_command(self, ctx: commands.Context) -> None:
        await self._work(Responder(ctx))

    async def do_work(self, interaction: discord.Interaction) -> None:
        await self._work(Responder(interaction), ephemeral=True)

    async def _work(self, responder: Responder, *, ephemeral: bool = False) -> None:
        guild_id = responder.guild_id
        settings = await self.bot.guard.ensure_player(guild_id, responder.user.id)
        earned, balance, multiplier = await self.bot.economy.work(guild_id, responder.user.id, settings)
        cooldown = int(settings["work_cooldown"])

        embed = theme.embed(
            title=f"{theme.ICON['work']} Shift complete",
            description=(
                f"**+{earned:,}** chips"
                + (f" at a **{theme.multiplier(multiplier)}** job multiplier" if multiplier > 1 else "")
                + f"\n{theme.ICON['balance']} Balance **{balance:,}**"
            ),
            color=theme.WIN,
            author=responder.user,
            footer=f"Next shift in {cooldown // 60}m · /jobs to raise your pay",
        )
        await responder.send(embed=embed, ephemeral=ephemeral)

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="jobs", aliases=["upgrades"], description="View and buy work upgrades.")
    async def jobs(self, ctx: commands.Context) -> None:
        responder = Responder(ctx)
        guild_id = responder.guild_id
        settings = await self.bot.guard.ensure_player(guild_id, responder.user.id)
        profile = await self.bot.economy.profile(guild_id, responder.user.id)
        level = int(profile.get("work_level", 0))
        step = float(settings["upgrade_multiplier_step"])
        upgrade = describe_upgrade(settings, level)

        ladder = []
        for offset in range(3):
            option = describe_upgrade(settings, level + offset)
            ladder.append(
                f"**{option['name']}** — {option['cost']:,} chips → x{option['multiplier']:g} work pay"
            )

        embed = theme.embed(
            title="🧰 Job upgrades",
            description=(
                f"Current level **{level}** · work pays **x{1 + step * level:g}**\n"
                f"Each upgrade adds **+{step:g}x** and costs "
                f"**{float(settings['upgrade_cost_growth']):g}x** more than the last.\n\n"
                + "\n".join(ladder)
            ),
            color=theme.WARN,
            author=responder.user,
            footer=f"/upgrade buys {upgrade['name']} for {upgrade['cost']:,} chips",
        )
        await responder.send(embed=embed)

    @commands.hybrid_command(name="upgrade", description="Buy the next work upgrade.")
    async def upgrade(self, ctx: commands.Context) -> None:
        responder = Responder(ctx)
        guild_id = responder.guild_id
        settings = await self.bot.guard.ensure_player(guild_id, responder.user.id)
        upgrade, balance = await self.bot.economy.buy_upgrade(guild_id, responder.user.id, settings)
        await responder.send(
            embed=theme.embed(
                title="⬆️ Promoted",
                description=(
                    f"You are now a **{upgrade['name']}** (level {upgrade['level']}).\n"
                    f"`/work` now pays **{theme.multiplier(upgrade['multiplier'])}**.\n"
                    f"{theme.ICON['balance']} Balance **{balance:,}**"
                ),
                color=theme.WIN,
                author=responder.user,
            )
        )

    # ------------------------------------------------------------------
    # transfers
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="give", aliases=["pay"], description="Send chips to another player.")
    @app_commands.describe(member="Who to pay.", amount="How much to send. Accepts 500, 10k, half, all.")
    async def give(self, ctx: commands.Context, member: discord.Member, amount: str) -> None:
        responder = Responder(ctx)
        if member.id == responder.user.id:
            raise CasinoError("Moving chips to yourself achieves nothing.")
        if member.bot:
            raise CasinoError("Bots have no use for chips.")

        guild_id = responder.guild_id
        await self.bot.guard.ensure_player(guild_id, responder.user.id)
        await self.bot.guard.ensure_player(guild_id, member.id)
        stake = await self.bot.guard.resolve_stake(guild_id, responder.user.id, amount, minimum=1)
        sender, recipient = await self.bot.economy.transfer(guild_id, responder.user.id, member.id, stake)

        await responder.send(
            embed=theme.embed(
                title="🤝 Chips sent",
                description=(
                    f"{responder.user.mention} → {member.mention}: **{stake:,}** chips\n\n"
                    f"{responder.user.display_name}: **{sender:,}**\n"
                    f"{member.display_name}: **{recipient:,}**"
                ),
                color=theme.BRAND,
            )
        )
        await self.bot.audit(
            responder.guild,
            "casino",
            theme.embed(
                title="🤝 Transfer",
                description=f"{responder.user.mention} sent **{stake:,}** chips to {member.mention}.",
                color=theme.INFO,
                timestamp=True,
            ),
        )

    # ------------------------------------------------------------------
    # history and timers
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="history", aliases=["transactions"], description="Page through your chip history.")
    async def history(self, ctx: commands.Context) -> None:
        responder = Responder(ctx)
        guild_id = responder.guild_id
        await self.bot.guard.ensure_player(guild_id, responder.user.id)
        total = await self.bot.economy.history_count(guild_id, responder.user.id)
        if total == 0:
            return await responder.send(
                embed=theme.embed(description="No chips have moved yet. Try `/daily`.", color=theme.PUSH)
            )

        pages: list[discord.Embed] = []
        for offset in range(0, min(total, LEDGER_PAGE_SIZE * 10), LEDGER_PAGE_SIZE):
            rows = await self.bot.economy.history(guild_id, responder.user.id, LEDGER_PAGE_SIZE, offset)
            lines = []
            for row in rows:
                icon = KIND_ICON.get(row["kind"], "•")
                when = parse_iso(row["created_at"])
                label = (row.get("game") or row["kind"]).title()
                stake = f" · stake {int(row['stake']):,}" if row["stake"] else ""
                note = f" · {row['note']}" if row.get("note") else ""
                lines.append(
                    f"{icon} **{label}** `{int(row['delta']):+,}`{stake}{note}\n"
                    f"　{theme.relative(when) if when else ''}"
                )
            pages.append(
                theme.embed(
                    title="📜 Chip history",
                    description="\n".join(lines),
                    color=theme.BRAND,
                    author=responder.user,
                    footer=f"{total:,} entries recorded",
                )
            )
        view = Paginator(pages, owner_id=responder.user.id)
        await responder.send(embed=view.current, view=view)

    @commands.hybrid_command(name="timers", aliases=["cooldowns", "cd"], description="See what is ready to claim.")
    async def timers_command(self, ctx: commands.Context) -> None:
        await self._timers(Responder(ctx))

    async def show_timers(self, interaction: discord.Interaction) -> None:
        await self._timers(Responder(interaction), ephemeral=True)

    async def _timers(self, responder: Responder, *, ephemeral: bool = False) -> None:
        guild_id = responder.guild_id
        await self.bot.guard.ensure_player(guild_id, responder.user.id)
        daily = await self.bot.guard.time_until_daily(guild_id, responder.user.id)
        work = await self.bot.guard.time_until_work(guild_id, responder.user.id)
        now = discord.utils.utcnow()

        lines = [
            f"{theme.ICON['daily']} **Daily** "
            + ("**ready now**" if daily is None else theme.relative(now + daily)),
            f"{theme.ICON['work']} **Work** "
            + ("**ready now**" if work is None else theme.relative(now + work)),
        ]
        await responder.send(
            embed=theme.embed(
                title=f"{theme.ICON['clock']} Timers",
                description="\n".join(lines),
                color=theme.INFO,
                author=responder.user,
            ),
            ephemeral=ephemeral,
        )


async def setup(bot: Any) -> None:
    await bot.add_cog(Economy(bot))
