"""Server configuration and admin tooling.

The old bot needed a source edit to change the staff role, and its economy
settings were spread across ``!economyconfig``, ``!casinoenable``,
``!maintenance``, ``!logtoggle`` and a hardcoded constant or two. Everything
tunable now lives behind ``/config``, with slash autocomplete listing the
keys and their current values, and every write validated against the
declared type.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from casino.config import GAME_KEYS, GUILD_DEFAULTS, LOG_KEYS
from casino.core.checks import staff_only
from casino.core.errors import CasinoError
from casino.db.economy import iso, utcnow
from casino.db.guilds import UnknownSetting
from casino.ui import theme
from casino.ui.responder import Responder
from casino.ui.views import Confirm

#: Keys a server owner is expected to edit, grouped for the /config display.
SETTING_GROUPS = {
    "Betting": ("min_bet", "max_bet", "rtp", "starting_balance"),
    "Faucets": ("daily_amount", "daily_streak_bonus", "work_min", "work_max", "work_cooldown"),
    "Jobs": ("upgrade_base_cost", "upgrade_cost_growth", "upgrade_multiplier_step"),
    "Channels and roles": (
        "casino_channel", "logs_channel", "welcome_channel", "goodbye_channel",
        "admin_role", "ticket_category", "ticket_support_role", "ticket_transcript_channel",
    ),
    "Messages": ("welcome_message", "goodbye_message", "ticket_message"),
    "State": ("maintenance", "event_name", "event_ends_at"),
}

CHANNEL_KEYS = {
    "casino_channel", "logs_channel", "welcome_channel", "goodbye_channel",
    "ticket_category", "ticket_transcript_channel",
}
ROLE_KEYS = {"admin_role", "ticket_support_role"}

ID_PATTERN = re.compile(r"\d{15,20}")


def coerce(key: str, raw: str) -> Any:
    """Turn a typed string into the right kind of value for ``key``.

    The default's type is the schema: if a setting defaults to an int, only an
    int can be stored in it. Channels and roles accept a mention or an id.
    """
    default = GUILD_DEFAULTS[key]
    text = raw.strip()

    if key in CHANNEL_KEYS or key in ROLE_KEYS:
        if text.lower() in ("none", "off", "clear", "unset"):
            return None
        match = ID_PATTERN.search(text)
        if match is None:
            raise CasinoError(f"`{key}` needs a channel or role mention, an id, or `none`.")
        return int(match.group())

    if isinstance(default, bool) or key == "maintenance":
        if text.lower() in ("on", "true", "yes", "1"):
            return 1
        if text.lower() in ("off", "false", "no", "0"):
            return 0
        raise CasinoError(f"`{key}` is a switch: use `on` or `off`.")

    if isinstance(default, int) and not isinstance(default, bool):
        cleaned = text.replace(",", "").replace("_", "")
        if not cleaned.lstrip("-").isdigit():
            raise CasinoError(f"`{key}` needs a whole number.")
        return int(cleaned)

    if isinstance(default, float):
        try:
            value = float(text)
        except ValueError:
            raise CasinoError(f"`{key}` needs a number, for example `0.97`.") from None
        if key == "rtp" and not 0.5 <= value <= 1.0:
            raise CasinoError("`rtp` must sit between 0.50 and 1.00. Above 1.0 the house loses chips forever.")
        return value

    if text.lower() in ("none", "clear", "unset"):
        return None
    return text


def render_value(guild: discord.Guild | None, key: str, value: Any) -> str:
    if value is None:
        return "*not set*"
    if key in CHANNEL_KEYS:
        return f"<#{int(value)}>"
    if key in ROLE_KEYS:
        return f"<@&{int(value)}>"
    if key == "maintenance":
        return "**on**" if value else "off"
    if isinstance(value, int):
        return f"{value:,}"
    return f"`{value}`" if isinstance(value, str) and len(str(value)) < 80 else str(value)[:80]


class Admin(commands.Cog, name="Admin"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    @commands.hybrid_group(name="config", description="View or change this server's settings.", invoke_without_command=True)
    @staff_only()
    async def config(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        await self.config_show(ctx)

    @config.command(name="show", description="Show every setting and its current value.")
    @staff_only()
    async def config_show(self, ctx: commands.Context) -> None:
        settings = await self.bot.guilds_store.settings(ctx.guild.id)
        games = await self.bot.guilds_store.games(ctx.guild.id)
        logs = await self.bot.guilds_store.logs(ctx.guild.id)

        embed = theme.embed(
            title=f"{theme.ICON['shield']} Server configuration",
            description="Change anything with `/config set <key> <value>`.",
            color=theme.BRAND,
            footer=f"{ctx.guild.name} · defaults apply to anything not set",
        )
        for group, keys in SETTING_GROUPS.items():
            embed.add_field(
                name=group,
                value="\n".join(f"`{key}` — {render_value(ctx.guild, key, settings.get(key))}" for key in keys),
                inline=False,
            )
        embed.add_field(
            name="Games",
            value=" · ".join(f"{'✅' if games.get(key, True) else '🚫'} {key}" for key in GAME_KEYS),
            inline=False,
        )
        embed.add_field(
            name="Log categories",
            value=" · ".join(f"{'✅' if logs.get(key, True) else '🚫'} {key}" for key in LOG_KEYS),
            inline=False,
        )
        await Responder(ctx).send(embed=embed)

    @config.command(name="set", description="Change one setting.")
    @app_commands.describe(key="Which setting.", value="The new value. Use 'none' to clear, 'on'/'off' for switches.")
    @staff_only()
    async def config_set(self, ctx: commands.Context, key: str, *, value: str) -> None:
        key = key.strip().lower()
        if key not in GUILD_DEFAULTS:
            raise CasinoError(f"`{key}` is not a setting. Run `/config show` to see the list.")
        parsed = coerce(key, value)
        try:
            await self.bot.guilds_store.update(ctx.guild.id, **{key: parsed})
        except UnknownSetting as exc:
            raise CasinoError(str(exc)) from None

        settings = await self.bot.guilds_store.settings(ctx.guild.id)
        if int(settings["min_bet"]) > int(settings["max_bet"]):
            raise CasinoError(
                f"That would leave `min_bet` ({int(settings['min_bet']):,}) above "
                f"`max_bet` ({int(settings['max_bet']):,}), which makes every bet impossible."
            )
        await Responder(ctx).send(
            embed=theme.success(f"`{key}` is now {render_value(ctx.guild, key, parsed)}.")
        )

    @config_set.autocomplete("key")
    async def _key_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        settings = await self.bot.guilds_store.settings(interaction.guild_id)
        current = current.lower()
        out = []
        for key in GUILD_DEFAULTS:
            if current and current not in key:
                continue
            value = settings.get(key)
            shown = "not set" if value is None else str(value)[:40]
            out.append(app_commands.Choice(name=f"{key} (now: {shown})"[:100], value=key))
        return out[:25]

    @config.command(name="reset", description="Restore every setting to its default.")
    @staff_only()
    async def config_reset(self, ctx: commands.Context) -> None:
        view = Confirm(owner_id=ctx.author.id, confirm_label="Reset settings")
        responder = Responder(ctx)
        await responder.send(
            embed=theme.embed(
                title="Reset all settings?",
                description="Balances and stats are untouched; only configuration returns to defaults.",
                color=theme.WARN,
            ),
            view=view,
        )
        await view.wait()
        if view.value:
            await self.bot.guilds_store.reset(ctx.guild.id)
            await responder.edit(embed=theme.success("Settings restored to defaults."), clear_view=True)

    @commands.hybrid_command(name="gametoggle", description="Switch a game on or off in this server.")
    @app_commands.describe(game="Which game.", state="on or off.")
    @app_commands.choices(
        game=[app_commands.Choice(name=key.title(), value=key) for key in GAME_KEYS],
        state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")],
    )
    @staff_only()
    async def gametoggle(self, ctx: commands.Context, game: str, state: str) -> None:
        await self.bot.guilds_store.set_game(ctx.guild.id, game, state == "on")
        await Responder(ctx).send(
            embed=theme.success(f"**{game.title()}** is now **{'available' if state == 'on' else 'off'}**.")
        )

    @commands.hybrid_command(name="logconfig", description="Choose the log channel or toggle a log category.")
    @app_commands.describe(channel="Where logs go.", category="Which category to toggle.", state="on or off.")
    @app_commands.choices(
        category=[app_commands.Choice(name=key.title(), value=key) for key in LOG_KEYS],
        state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")],
    )
    @staff_only()
    async def logconfig(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
        category: str | None = None,
        state: str | None = None,
    ) -> None:
        changes = []
        if channel is not None:
            await self.bot.guilds_store.update(ctx.guild.id, logs_channel=channel.id)
            changes.append(f"logs go to {channel.mention}")
        if category is not None and state is not None:
            await self.bot.guilds_store.set_log(ctx.guild.id, category, state == "on")
            changes.append(f"`{category}` logging is **{state}**")
        if not changes:
            logs = await self.bot.guilds_store.logs(ctx.guild.id)
            settings = await self.bot.guilds_store.settings(ctx.guild.id)
            return await Responder(ctx).send(
                embed=theme.embed(
                    title="📜 Logging",
                    description=(
                        f"**Channel** {render_value(ctx.guild, 'logs_channel', settings.get('logs_channel'))}\n\n"
                        + "\n".join(f"{'✅' if value else '🚫'} `{key}`" for key, value in logs.items())
                    ),
                    color=theme.INFO,
                    footer="/logconfig channel:#log category:messages state:off",
                )
            )
        await Responder(ctx).send(embed=theme.success(" · ".join(changes) + "."))

    # ------------------------------------------------------------------
    # chips
    # ------------------------------------------------------------------

    @commands.hybrid_group(name="bank", description="Grant, take, or set a player's chips.", invoke_without_command=True)
    @staff_only()
    async def bank(self, ctx: commands.Context) -> None:
        await Responder(ctx).send(
            embed=theme.embed(
                title="Chip tools",
                description=theme.bullet_list(
                    [
                        "`/bank add @player 5000`",
                        "`/bank take @player 5000`",
                        "`/bank set @player 2500`",
                        "`/bank joblevel @player 3`",
                    ]
                ),
                color=theme.BRAND,
            )
        )

    @bank.command(name="add", description="Grant chips to a player.")
    @staff_only()
    async def bank_add(self, ctx: commands.Context, member: discord.Member, amount: int) -> None:
        if amount <= 0:
            raise CasinoError("Amount must be positive.")
        await self.bot.guard.ensure_player(ctx.guild.id, member.id)
        balance = await self.bot.economy.adjust(
            ctx.guild.id, member.id, amount, kind="admin", note=f"granted by {ctx.author}"
        )
        await Responder(ctx).send(
            embed=theme.success(f"Gave {member.mention} **{amount:,}** chips. New balance **{balance:,}**.")
        )
        await self._log(ctx, f"Granted **{amount:,}** chips to {member.mention}.")

    @bank.command(name="take", description="Remove chips from a player.")
    @staff_only()
    async def bank_take(self, ctx: commands.Context, member: discord.Member, amount: int) -> None:
        if amount <= 0:
            raise CasinoError("Amount must be positive.")
        await self.bot.guard.ensure_player(ctx.guild.id, member.id)
        balance = await self.bot.economy.adjust(
            ctx.guild.id, member.id, -amount, kind="admin",
            note=f"removed by {ctx.author}", allow_negative=True,
        )
        await Responder(ctx).send(
            embed=theme.success(f"Took **{amount:,}** chips from {member.mention}. New balance **{balance:,}**.")
        )
        await self._log(ctx, f"Removed **{amount:,}** chips from {member.mention}.")

    @bank.command(name="set", description="Set a player's balance exactly.")
    @staff_only()
    async def bank_set(self, ctx: commands.Context, member: discord.Member, amount: int) -> None:
        if amount < 0:
            raise CasinoError("A balance cannot be negative.")
        await self.bot.guard.ensure_player(ctx.guild.id, member.id)
        balance = await self.bot.economy.set_balance(ctx.guild.id, member.id, amount, f"set by {ctx.author}")
        await Responder(ctx).send(embed=theme.success(f"{member.mention} now has **{balance:,}** chips."))
        await self._log(ctx, f"Set {member.mention}'s balance to **{balance:,}**.")

    @bank.command(name="joblevel", description="Set a player's work level.")
    @staff_only()
    async def bank_joblevel(self, ctx: commands.Context, member: discord.Member, level: int) -> None:
        await self.bot.guard.ensure_player(ctx.guild.id, member.id)
        value = await self.bot.economy.set_work_level(ctx.guild.id, member.id, level)
        settings = await self.bot.guilds_store.settings(ctx.guild.id)
        multiplier = 1 + float(settings["upgrade_multiplier_step"]) * value
        await Responder(ctx).send(
            embed=theme.success(f"{member.mention} is job level **{value}** (work pays {theme.multiplier(multiplier)}).")
        )

    @commands.hybrid_command(name="cleartimers", description="Clear a player's daily and work cooldowns.")
    @staff_only()
    async def cleartimers(self, ctx: commands.Context, member: discord.Member) -> None:
        await self.bot.economy.clear_cooldowns(ctx.guild.id, member.id)
        await Responder(ctx).send(embed=theme.success(f"Cleared {member.mention}'s daily and work timers."))

    # ------------------------------------------------------------------
    # resets
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="resetplayer", description="Wipe one player's casino account.")
    @staff_only()
    async def resetplayer(self, ctx: commands.Context, member: discord.Member) -> None:
        settings = await self.bot.guilds_store.settings(ctx.guild.id)
        responder = Responder(ctx)
        view = Confirm(owner_id=ctx.author.id, confirm_label=f"Reset {member.display_name}", confirm_emoji="♻️")
        await responder.send(
            embed=theme.embed(
                title="♻️ Confirm account reset",
                description=(
                    f"This clears {member.mention}'s balance, job level, statistics, achievements, "
                    "cosmetics, challenge progress and chip history, and puts them back on "
                    f"**{int(settings['starting_balance']):,}** chips.\n\n"
                    "Their Discord account and roles are untouched, and an active "
                    "self-exclusion is deliberately kept."
                ),
                color=theme.DANGER,
            ),
            view=view,
        )
        await view.wait()
        if not view.value:
            return
        balance = await self.bot.economy.reset_player(
            ctx.guild.id, member.id, int(settings["starting_balance"])
        )
        await responder.edit(
            embed=theme.success(f"{member.mention} has been reset to **{balance:,}** chips."), clear_view=True
        )
        await self._log(ctx, f"Fully reset {member.mention}.")

    @commands.hybrid_command(name="reseteconomy", description="Wipe every player's casino account in this server.")
    @staff_only()
    async def reseteconomy(self, ctx: commands.Context) -> None:
        settings = await self.bot.guilds_store.settings(ctx.guild.id)
        player_ids = await self.bot.economy.player_ids(ctx.guild.id)
        responder = Responder(ctx)
        view = Confirm(owner_id=ctx.author.id, confirm_label=f"Reset {len(player_ids)} players", confirm_emoji="💣")
        await responder.send(
            embed=theme.embed(
                title="💣 Reset the whole economy?",
                description=(
                    f"**{len(player_ids)}** player(s) would go back to "
                    f"**{int(settings['starting_balance']):,}** chips with no history. "
                    "This cannot be undone."
                ),
                color=theme.DANGER,
            ),
            view=view,
        )
        await view.wait()
        if not view.value:
            return
        for user_id in player_ids:
            await self.bot.economy.reset_player(ctx.guild.id, user_id, int(settings["starting_balance"]))
        await responder.edit(
            embed=theme.success(f"Reset **{len(player_ids)}** player(s)."), clear_view=True
        )
        await self._log(ctx, f"Reset the entire economy ({len(player_ids)} players).")

    # ------------------------------------------------------------------
    # access and state
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="casinoban", description="Block or unblock a player's casino access.")
    @app_commands.describe(member="Who.", duration="e.g. 12h, 7d, or 'off' to lift.")
    @staff_only()
    async def casinoban(self, ctx: commands.Context, member: discord.Member, duration: str = "off") -> None:
        from casino.cogs.moderation import parse_duration

        if duration.strip().lower() in ("off", "none", "lift"):
            await self.bot.progression.set_casino_ban(ctx.guild.id, member.id, None)
            await Responder(ctx).send(embed=theme.success(f"{member.mention} can play again."))
            return await self._log(ctx, f"Lifted the casino ban on {member.mention}.")

        span = parse_duration(duration)
        if span is None:
            raise CasinoError("I could not read that duration. Try `12h`, `3d` or `off`.")
        until = utcnow() + span
        await self.bot.progression.set_casino_ban(ctx.guild.id, member.id, until)
        await Responder(ctx).send(
            embed=theme.success(
                f"{member.mention} cannot play until {discord.utils.format_dt(until, 'f')}."
            )
        )
        await self._log(ctx, f"Casino-banned {member.mention} until {discord.utils.format_dt(until, 'f')}.")

    @commands.hybrid_command(name="maintenance", description="Close or reopen the casino.")
    @app_commands.choices(
        state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]
    )
    @staff_only()
    async def maintenance(self, ctx: commands.Context, state: str) -> None:
        await self.bot.guilds_store.update(ctx.guild.id, maintenance=1 if state == "on" else 0)
        await Responder(ctx).send(
            embed=theme.embed(
                title="🔧 Maintenance " + ("on" if state == "on" else "off"),
                description=(
                    "Games are closed; wallets, profiles and leaderboards still work."
                    if state == "on"
                    else "The casino is open again."
                ),
                color=theme.WARN if state == "on" else theme.WIN,
            )
        )

    @commands.hybrid_group(name="event", description="Announce a server event.", invoke_without_command=True)
    @staff_only()
    async def event(self, ctx: commands.Context) -> None:
        settings = await self.bot.guilds_store.settings(ctx.guild.id)
        name = settings.get("event_name")
        if not name:
            return await Responder(ctx).send(
                embed=theme.embed(description="No event is running. Start one with `/event start`.", color=theme.PUSH)
            )
        from casino.db.economy import parse_iso

        ends = parse_iso(settings.get("event_ends_at"))
        await Responder(ctx).send(
            embed=theme.embed(
                title=f"📣 {name}",
                description=f"Ends {theme.relative(ends)}." if ends else "No end time set.",
                color=theme.GOLD,
            )
        )

    @event.command(name="start", description="Announce an event for a number of hours.")
    @staff_only()
    async def event_start(self, ctx: commands.Context, hours: int, *, name: str) -> None:
        if not 1 <= hours <= 720:
            raise CasinoError("Events run from 1 to 720 hours.")
        ends = utcnow() + timedelta(hours=hours)
        await self.bot.guilds_store.update(ctx.guild.id, event_name=name[:100], event_ends_at=iso(ends))
        await Responder(ctx).send(
            embed=theme.embed(
                title=f"📣 {name}",
                description=(
                    f"Announced until {discord.utils.format_dt(ends, 'f')}.\n\n"
                    "Events are announcements only — they never change the odds or payouts, "
                    "so the published return to player stays true."
                ),
                color=theme.GOLD,
            )
        )

    @event.command(name="stop", description="End the current event.")
    @staff_only()
    async def event_stop(self, ctx: commands.Context) -> None:
        await self.bot.guilds_store.update(ctx.guild.id, event_name=None, event_ends_at=None)
        await Responder(ctx).send(embed=theme.success("Event ended."))

    # ------------------------------------------------------------------
    # analytics
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="analytics", aliases=["casinostats"], description="How the house is doing.")
    @staff_only()
    async def analytics(self, ctx: commands.Context) -> None:
        totals = await self.bot.economy.guild_totals(ctx.guild.id)
        players = int(totals.get("players", 0))
        wagered = int(totals.get("wagered", 0))
        net = int(totals.get("net", 0))
        # Players' net profit is the house's loss, so the realised return to
        # player is 1 + (player net / total staked).
        realised = (1 + net / wagered) if wagered else 0.0
        settings = await self.bot.guilds_store.settings(ctx.guild.id)

        embed = theme.embed(
            title="📊 Casino analytics",
            description=f"**{ctx.guild.name}** · {players:,} player(s) with accounts",
            color=theme.INFO,
            footer="Realised return converges on the configured value as volume grows.",
        )
        embed.add_field(name="Chips in circulation", value=f"{int(totals.get('chips', 0)):,}", inline=True)
        embed.add_field(name="Rounds played", value=f"{int(totals.get('games', 0)):,}", inline=True)
        embed.add_field(name="Total staked", value=f"{wagered:,}", inline=True)
        embed.add_field(name="Player net", value=theme.signed(net), inline=True)
        embed.add_field(
            name="Return to player",
            value=f"{realised * 100:.2f}% realised\n{float(settings['rtp']) * 100:.1f}% configured",
            inline=True,
        )
        embed.add_field(name="Biggest single win", value=f"{int(totals.get('biggest_win', 0)):,}", inline=True)

        popular = totals.get("games_by_popularity") or []
        if popular:
            embed.add_field(
                name="By popularity",
                value="\n".join(
                    f"**{row['game'].title()}** — {int(row['plays']):,} rounds · "
                    f"{int(row['wagered']):,} staked · {theme.signed(row['net'])} to players"
                    for row in popular[:8]
                ),
                inline=False,
            )
        await Responder(ctx).send(embed=embed)

    # ------------------------------------------------------------------

    async def _log(self, ctx: commands.Context, description: str) -> None:
        await self.bot.audit(
            ctx.guild,
            "casino",
            theme.embed(
                title=f"{theme.ICON['shield']} Admin action",
                description=f"{ctx.author.mention}: {description}",
                color=theme.WARN,
                timestamp=True,
            ),
        )


async def setup(bot: Any) -> None:
    await bot.add_cog(Admin(bot))
