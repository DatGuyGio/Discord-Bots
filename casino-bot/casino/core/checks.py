"""Permission checks.

The old bot hardcoded a single role id in the source (``ADMIN_ROLE_ID =
1519703156492337182``), so every other server that added the bot had no way
to grant staff access short of editing Python. The staff role is now a
per-guild setting, with real Discord permissions as the always-available
fallback.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

import discord
from discord.ext import commands

from casino.core.errors import NotPermitted

T = TypeVar("T")


async def is_staff(bot: Any, member: discord.Member | discord.User | None) -> bool:
    """True for guild administrators, the configured staff role, or bot owners."""
    if member is None:
        return False
    if await bot.is_owner(member):
        return True
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    settings = await bot.guilds_store.settings(member.guild.id)
    role_id = settings.get("admin_role")
    return bool(role_id) and any(role.id == int(role_id) for role in member.roles)


def staff_only() -> Callable[[T], T]:
    """Command check restricting a command to staff."""

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise NotPermitted("This command only works inside a server.")
        if await is_staff(ctx.bot, ctx.author):
            return True
        raise NotPermitted(
            "You need **Manage Server**, or the staff role set with `/config staffrole`."
        )

    return commands.check(predicate)


def guild_only() -> Callable[[T], T]:
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise NotPermitted("This command only works inside a server.")
        return True

    return commands.check(predicate)


async def can_act_on(moderator: discord.Member, target: discord.Member) -> str | None:
    """Sanity checks before a moderation action. Returns a reason to refuse.

    The old commands would happily try to ban the server owner or someone
    above the moderator in the role hierarchy and then report a generic
    Forbidden error from Discord. Checking first gives a useful message.
    """
    if moderator.id == target.id:
        return "You cannot use a moderation command on yourself."
    if target.id == target.guild.owner_id:
        return "That member owns the server."
    if target.bot and target.id == moderator.guild.me.id:
        return "I am not going to do that to myself."
    if moderator.id != moderator.guild.owner_id and target.top_role >= moderator.top_role:
        return f"{target.mention} has a role at or above yours."
    if target.top_role >= moderator.guild.me.top_role:
        return (
            f"{target.mention} is above me in the role list, so Discord will not let me act. "
            "Move my role higher."
        )
    return None
