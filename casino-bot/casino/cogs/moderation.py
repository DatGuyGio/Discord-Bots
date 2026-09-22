"""Moderation.

Same commands as before, with the rough edges taken off:

* Warnings are scoped per server rather than globally, so a warning in one
  server no longer shows up in every other server the bot is in.
* Every action is checked against the role hierarchy *before* it is
  attempted, so you get "that member is above me in the role list" instead of
  a bare Forbidden error.
* Staff are whoever has Manage Server or the configured staff role, rather
  than a role id hardcoded in the source.
* ``/purge`` can filter by member, bots, or text, instead of only a count.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from casino.core.checks import can_act_on, staff_only
from casino.core.errors import CasinoError, NotPermitted
from casino.db.economy import parse_iso
from casino.ui import theme
from casino.ui.responder import Responder
from casino.ui.views import Confirm, Paginator

MAX_TIMEOUT = timedelta(days=28)
MAX_PURGE = 200
DURATION = re.compile(r"(\d+)\s*([smhdw])")

UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_duration(text: str) -> timedelta | None:
    """``"1h30m"``, ``"10m"``, ``"2d"`` -> a timedelta.

    The old parser only accepted a single unit with no spaces, so ``1h30m``
    was rejected outright.
    """
    if not text:
        return None
    matches = DURATION.findall(text.strip().lower())
    if not matches:
        return None
    total = timedelta()
    for amount, unit in matches:
        total += timedelta(**{UNITS[unit]: int(amount)})
    return total if total.total_seconds() > 0 else None


class Moderation(commands.Cog, name="Moderation"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def _log(self, guild: discord.Guild, title: str, description: str, *, color: discord.Color, fields: list[tuple[str, str]] | None = None) -> None:
        embed = theme.embed(title=title, description=description, color=color, timestamp=True)
        for name, value in fields or []:
            embed.add_field(name=name, value=value[:1024], inline=False)
        await self.bot.audit(guild, "moderation", embed)

    async def _guard(self, ctx: commands.Context, member: discord.Member) -> None:
        reason = await can_act_on(ctx.author, member)  # type: ignore[arg-type]
        if reason:
            raise NotPermitted(reason)

    # ------------------------------------------------------------------
    # warnings
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="warn", description="Warn a member.")
    @app_commands.describe(member="Who to warn.", reason="Why.")
    @staff_only()
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason given") -> None:
        await self._guard(ctx, member)
        total = await self.bot.moderation.warn(ctx.guild.id, member.id, ctx.author.id, reason)
        await Responder(ctx).send(
            embed=theme.embed(
                title=f"{theme.ICON['warn']} Member warned",
                description=f"{member.mention} now has **{total}** warning(s).\n**Reason** {reason}",
                color=theme.WARN,
            )
        )
        try:
            await member.send(
                embed=theme.embed(
                    title=f"{theme.ICON['warn']} Warning in {ctx.guild.name}",
                    description=reason,
                    color=theme.WARN,
                    footer=f"Warning {total}",
                )
            )
        except discord.Forbidden:
            pass  # DMs closed; not worth reporting
        await self._log(
            ctx.guild, f"{theme.ICON['warn']} Warning issued",
            f"{member.mention} (`{member.id}`) warned by {ctx.author.mention}.",
            color=theme.WARN, fields=[("Reason", reason), ("Total", str(total))],
        )

    @commands.hybrid_command(name="warnings", description="List a member's warnings.")
    @app_commands.describe(member="Whose warnings to list.")
    async def warnings(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        from casino.core.checks import is_staff

        target = member or ctx.author
        if target.id != ctx.author.id and not await is_staff(self.bot, ctx.author):
            raise NotPermitted("You can only look up your own warnings.")

        entries = await self.bot.moderation.warnings(ctx.guild.id, target.id)
        if not entries:
            return await Responder(ctx).send(
                embed=theme.success(f"{target.mention} has no warnings.")
            )

        pages = []
        for start in range(0, len(entries), 5):
            chunk = entries[start : start + 5]
            lines = []
            for entry in chunk:
                moderator = ctx.guild.get_member(int(entry["moderator_id"]))
                when = parse_iso(entry["created_at"])
                lines.append(
                    f"`#{entry['id']}` {entry['reason']}\n"
                    f"　by {moderator.mention if moderator else 'unknown'} "
                    f"{theme.relative(when) if when else ''}"
                )
            pages.append(
                theme.embed(
                    title=f"{theme.ICON['warn']} Warnings for {target.display_name}",
                    description="\n".join(lines),
                    color=theme.WARN,
                    footer=f"{len(entries)} total",
                )
            )
        view = Paginator(pages, owner_id=ctx.author.id)
        await Responder(ctx).send(embed=view.current, view=view)

    @commands.hybrid_command(name="clearwarnings", description="Delete all of a member's warnings.")
    @staff_only()
    async def clearwarnings(self, ctx: commands.Context, member: discord.Member) -> None:
        count = await self.bot.moderation.clear_warnings(ctx.guild.id, member.id)
        await Responder(ctx).send(
            embed=theme.success(f"Cleared **{count}** warning(s) for {member.mention}.")
        )
        await self._log(
            ctx.guild, "🧽 Warnings cleared",
            f"{ctx.author.mention} cleared {count} warning(s) for {member.mention}.",
            color=theme.INFO,
        )

    @commands.hybrid_command(name="delwarn", description="Delete one warning by its id.")
    @staff_only()
    async def delwarn(self, ctx: commands.Context, warning_id: int) -> None:
        if not await self.bot.moderation.delete_warning(ctx.guild.id, warning_id):
            raise CasinoError(f"No warning `#{warning_id}` in this server.")
        await Responder(ctx).send(embed=theme.success(f"Deleted warning `#{warning_id}`."))

    # ------------------------------------------------------------------
    # timeouts, kicks, bans
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="timeout", aliases=["mute"], description="Time a member out.")
    @app_commands.describe(member="Who.", duration="How long, e.g. 10m, 1h30m, 2d.", reason="Why.")
    @staff_only()
    async def timeout(self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason given") -> None:
        await self._guard(ctx, member)
        span = parse_duration(duration)
        if span is None:
            raise CasinoError("I could not read that duration. Try `10m`, `1h30m` or `2d`.")
        if span > MAX_TIMEOUT:
            raise CasinoError("Discord caps timeouts at 28 days.")
        try:
            await member.timeout(span, reason=f"{reason} · by {ctx.author}")
        except discord.Forbidden:
            raise CasinoError("Discord refused: check my Moderate Members permission and role position.") from None

        until = discord.utils.utcnow() + span
        await Responder(ctx).send(
            embed=theme.embed(
                title="🔇 Timed out",
                description=f"{member.mention} until {discord.utils.format_dt(until, 'f')} ({theme.relative(until)}).\n**Reason** {reason}",
                color=theme.WARN,
            )
        )
        await self._log(
            ctx.guild, "🔇 Member timed out",
            f"{member.mention} (`{member.id}`) by {ctx.author.mention} for {duration}.",
            color=theme.WARN, fields=[("Reason", reason)],
        )

    @commands.hybrid_command(name="untimeout", aliases=["unmute"], description="Lift a timeout.")
    @staff_only()
    async def untimeout(self, ctx: commands.Context, member: discord.Member) -> None:
        try:
            await member.timeout(None, reason=f"Lifted by {ctx.author}")
        except discord.Forbidden:
            raise CasinoError("Discord refused: check my permissions and role position.") from None
        await Responder(ctx).send(embed=theme.success(f"{member.mention} can talk again."))
        await self._log(
            ctx.guild, "🔊 Timeout lifted", f"{member.mention} by {ctx.author.mention}.", color=theme.WIN
        )

    @commands.hybrid_command(name="kick", description="Kick a member.")
    @staff_only()
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason given") -> None:
        await self._guard(ctx, member)
        try:
            await member.send(
                embed=theme.embed(title=f"You were kicked from {ctx.guild.name}", description=reason, color=theme.LOSE)
            )
        except discord.Forbidden:
            pass
        try:
            await member.kick(reason=f"{reason} · by {ctx.author}")
        except discord.Forbidden:
            raise CasinoError("Discord refused: check my Kick Members permission and role position.") from None
        await Responder(ctx).send(
            embed=theme.embed(title="👢 Kicked", description=f"{member.mention}\n**Reason** {reason}", color=theme.LOSE)
        )
        await self._log(
            ctx.guild, "👢 Member kicked", f"{member.mention} (`{member.id}`) by {ctx.author.mention}.",
            color=theme.LOSE, fields=[("Reason", reason)],
        )

    @commands.hybrid_command(name="ban", description="Ban a member.")
    @app_commands.describe(delete_days="Days of their recent messages to delete (0-7).")
    @staff_only()
    async def ban(self, ctx: commands.Context, member: discord.Member, delete_days: int = 0, *, reason: str = "No reason given") -> None:
        await self._guard(ctx, member)
        responder = Responder(ctx)
        view = Confirm(owner_id=ctx.author.id, confirm_label=f"Ban {member.display_name}", confirm_emoji="🔨")
        await responder.send(
            embed=theme.embed(
                title="🔨 Confirm ban",
                description=(
                    f"Ban {member.mention} (`{member.id}`)?\n**Reason** {reason}\n"
                    f"**Message deletion** {max(0, min(7, delete_days))} day(s)"
                ),
                color=theme.DANGER,
            ),
            view=view,
        )
        await view.wait()
        if not view.value:
            return

        try:
            await member.send(
                embed=theme.embed(title=f"You were banned from {ctx.guild.name}", description=reason, color=theme.LOSE)
            )
        except discord.Forbidden:
            pass
        try:
            await member.ban(
                reason=f"{reason} · by {ctx.author}",
                delete_message_seconds=max(0, min(7, delete_days)) * 86_400,
            )
        except discord.Forbidden:
            raise CasinoError("Discord refused: check my Ban Members permission and role position.") from None

        await responder.edit(
            embed=theme.embed(
                title="🔨 Banned", description=f"{member.mention}\n**Reason** {reason}", color=theme.LOSE
            ),
            clear_view=True,
        )
        await self._log(
            ctx.guild, "🔨 Member banned", f"{member.mention} (`{member.id}`) by {ctx.author.mention}.",
            color=theme.LOSE, fields=[("Reason", reason)],
        )

    @commands.hybrid_command(name="unban", description="Unban a user by id.")
    @staff_only()
    async def unban(self, ctx: commands.Context, user_id: str, *, reason: str = "No reason given") -> None:
        if not user_id.isdigit():
            raise CasinoError("Give the numeric user id. Right-click the user in the ban list to copy it.")
        try:
            user = await self.bot.fetch_user(int(user_id))
            await ctx.guild.unban(user, reason=f"{reason} · by {ctx.author}")
        except discord.NotFound:
            raise CasinoError("That user is not banned here.") from None
        except discord.Forbidden:
            raise CasinoError("I do not have permission to unban.") from None
        await Responder(ctx).send(embed=theme.success(f"Unbanned **{user}** (`{user.id}`)."))
        await self._log(
            ctx.guild, "✅ Member unbanned", f"**{user}** (`{user.id}`) by {ctx.author.mention}.", color=theme.WIN
        )

    # ------------------------------------------------------------------
    # channel tools
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="purge", aliases=["clear"], description="Bulk delete recent messages.")
    @app_commands.describe(
        amount=f"How many messages to scan (1-{MAX_PURGE}).",
        member="Only delete messages from this member.",
        contains="Only delete messages containing this text.",
        bots="Only delete messages from bots.",
    )
    @staff_only()
    async def purge(
        self,
        ctx: commands.Context,
        amount: int,
        member: discord.Member | None = None,
        contains: str | None = None,
        bots: bool = False,
    ) -> None:
        if not 1 <= amount <= MAX_PURGE:
            raise CasinoError(f"Choose between 1 and {MAX_PURGE}.")

        def matches(message: discord.Message) -> bool:
            if member and message.author.id != member.id:
                return False
            if bots and not message.author.bot:
                return False
            if contains and contains.lower() not in (message.content or "").lower():
                return False
            return True

        if ctx.interaction is not None:
            await ctx.defer(ephemeral=True)
        try:
            deleted = await ctx.channel.purge(
                limit=amount, check=matches, before=ctx.message if ctx.message else None
            )
        except discord.Forbidden:
            raise CasinoError("I need Manage Messages here.") from None

        filters = []
        if member:
            filters.append(f"from {member.mention}")
        if bots:
            filters.append("from bots")
        if contains:
            filters.append(f"containing `{contains}`")
        detail = (" " + ", ".join(filters)) if filters else ""

        note = await ctx.send(
            embed=theme.success(f"Deleted **{len(deleted)}** message(s){detail}."), ephemeral=True
        )
        await self._log(
            ctx.guild, "🧹 Messages purged",
            f"{ctx.author.mention} deleted **{len(deleted)}** message(s) in {ctx.channel.mention}{detail}.",
            color=theme.INFO,
        )
        if ctx.interaction is None and note is not None:
            await asyncio.sleep(5)
            try:
                await note.delete()
            except discord.HTTPException:
                pass

    @commands.hybrid_command(name="slowmode", description="Set a channel's slowmode.")
    @app_commands.describe(seconds="0 to turn it off, up to 21600 (6h).", channel="Which channel.")
    @staff_only()
    async def slowmode(self, ctx: commands.Context, seconds: int, channel: discord.TextChannel | None = None) -> None:
        if not 0 <= seconds <= 21_600:
            raise CasinoError("Slowmode runs from 0 to 21600 seconds.")
        target = channel or ctx.channel
        try:
            await target.edit(slowmode_delay=seconds)
        except discord.Forbidden:
            raise CasinoError("I need Manage Channels there.") from None
        await Responder(ctx).send(
            embed=theme.success(
                f"{target.mention} slowmode is now **{seconds}s**." if seconds else f"Slowmode off in {target.mention}."
            )
        )

    @commands.hybrid_command(name="lock", description="Stop @everyone posting in a channel.")
    @staff_only()
    async def lock(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_lock(ctx, channel or ctx.channel, locked=True)

    @commands.hybrid_command(name="unlock", description="Let @everyone post again.")
    @staff_only()
    async def unlock(self, ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
        await self._set_lock(ctx, channel or ctx.channel, locked=False)

    async def _set_lock(self, ctx: commands.Context, channel: discord.TextChannel, *, locked: bool) -> None:
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False if locked else None
        try:
            await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        except discord.Forbidden:
            raise CasinoError("I need Manage Roles on that channel.") from None
        await Responder(ctx).send(
            embed=theme.embed(
                title="🔒 Channel locked" if locked else "🔓 Channel unlocked",
                description=f"{channel.mention} is now {'locked' if locked else 'open'}.",
                color=theme.LOSE if locked else theme.WIN,
            )
        )
        await self._log(
            ctx.guild,
            "🔒 Channel locked" if locked else "🔓 Channel unlocked",
            f"{ctx.author.mention} {'locked' if locked else 'unlocked'} {channel.mention}.",
            color=theme.WARN,
        )

    @commands.hybrid_command(name="nick", description="Change or clear a member's nickname.")
    @staff_only()
    async def nick(self, ctx: commands.Context, member: discord.Member, *, nickname: str | None = None) -> None:
        try:
            await member.edit(nick=nickname, reason=f"Changed by {ctx.author}")
        except discord.Forbidden:
            raise CasinoError("I need Manage Nicknames and a higher role than them.") from None
        await Responder(ctx).send(
            embed=theme.success(
                f"{member.mention} is now **{nickname}**." if nickname else f"Reset {member.mention}'s nickname."
            )
        )


async def setup(bot: Any) -> None:
    await bot.add_cog(Moderation(bot))
