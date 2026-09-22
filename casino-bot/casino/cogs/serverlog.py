"""Welcome messages and server audit logging.

Two upgrades over the old event handlers. Deletions and bans are attributed
by reading Discord's own audit log, so a log line says *who* deleted the
message rather than only whose message it was. And every handler checks its
category switch first, so turning off ``messages`` really does stop the
message spam instead of only hiding some of it.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands

from casino.ui import theme

log = logging.getLogger(__name__)


def render_template(template: str, member: discord.Member) -> str:
    replacements = {
        "{user}": member.mention,
        "{username}": discord.utils.escape_markdown(member.name),
        "{displayname}": discord.utils.escape_markdown(member.display_name),
        "{server}": discord.utils.escape_markdown(member.guild.name),
        "{membercount}": str(member.guild.member_count or 0),
        "{userid}": str(member.id),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


class ServerLog(commands.Cog, name="Server log"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def _actor(
        self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int | None = None
    ) -> discord.abc.User | None:
        """Best-effort "who did this" from the guild audit log."""
        if not guild.me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                if target_id is None or getattr(entry.target, "id", None) == target_id:
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    # -- membership --------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        settings = await self.bot.guilds_store.settings(member.guild.id)
        channel_id = settings.get("welcome_channel")
        if channel_id:
            channel = member.guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(
                        embed=theme.embed(
                            title="👋 Welcome",
                            description=render_template(settings.get("welcome_message", ""), member),
                            color=theme.WIN,
                            thumbnail=member.display_avatar.url,
                            footer=f"Member #{member.guild.member_count}",
                        )
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
        await self.bot.audit(
            member.guild,
            "members",
            theme.embed(
                title="📥 Member joined",
                description=f"{member.mention} (`{member.id}`)",
                color=theme.WIN,
                timestamp=True,
                footer=f"Account created {member.created_at:%Y-%m-%d}",
            ),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        settings = await self.bot.guilds_store.settings(member.guild.id)
        channel_id = settings.get("goodbye_channel")
        if channel_id:
            channel = member.guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(
                        embed=theme.embed(
                            description=render_template(settings.get("goodbye_message", ""), member),
                            color=theme.PUSH,
                        )
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
        await self.bot.audit(
            member.guild,
            "members",
            theme.embed(
                title="📤 Member left",
                description=f"**{member}** (`{member.id}`)",
                color=theme.WARN,
                timestamp=True,
            ),
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.nick != after.nick:
            await self.bot.audit(
                after.guild,
                "members",
                theme.embed(
                    title="✏️ Nickname changed",
                    description=(
                        f"{after.mention}\n"
                        f"**Before** {before.nick or '*none*'}\n**After** {after.nick or '*none*'}"
                    ),
                    color=theme.INFO,
                    timestamp=True,
                ),
            )
        before_roles = {role.id for role in before.roles}
        after_roles = {role.id for role in after.roles}
        if before_roles != after_roles:
            added = [r.mention for r in after.roles if r.id not in before_roles and not r.is_default()]
            removed = [r.mention for r in before.roles if r.id not in after_roles and not r.is_default()]
            actor = await self._actor(after.guild, discord.AuditLogAction.member_role_update, after.id)
            embed = theme.embed(
                title="🎭 Roles changed",
                description=f"{after.mention}" + (f" · by {actor.mention}" if actor else ""),
                color=theme.INFO,
                timestamp=True,
            )
            if added:
                embed.add_field(name="Added", value=", ".join(added)[:1024], inline=False)
            if removed:
                embed.add_field(name="Removed", value=", ".join(removed)[:1024], inline=False)
            await self.bot.audit(after.guild, "roles", embed)

    # -- messages ----------------------------------------------------------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        actor = await self._actor(message.guild, discord.AuditLogAction.message_delete, message.author.id)
        embed = theme.embed(
            title="🗑️ Message deleted",
            description=(
                f"By **{message.author}** in {message.channel.mention}"
                + (f" · deleted by {actor.mention}" if actor and actor.id != message.author.id else "")
            ),
            color=theme.LOSE,
            timestamp=True,
        )
        embed.add_field(
            name="Content",
            value=(message.content or "*no text — attachment or embed*")[:1024],
            inline=False,
        )
        if message.attachments:
            embed.add_field(
                name="Attachments",
                value="\n".join(a.filename for a in message.attachments)[:1024],
                inline=False,
            )
        await self.bot.audit(message.guild, "messages", embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None or before.author.bot or before.content == after.content:
            return
        embed = theme.embed(
            title="✏️ Message edited",
            description=f"By {before.author.mention} in {before.channel.mention} · [jump]({after.jump_url})",
            color=theme.WARN,
            timestamp=True,
        )
        embed.add_field(name="Before", value=(before.content or "*empty*")[:1024], inline=False)
        embed.add_field(name="After", value=(after.content or "*empty*")[:1024], inline=False)
        await self.bot.audit(before.guild, "messages", embed)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages or messages[0].guild is None:
            return
        await self.bot.audit(
            messages[0].guild,
            "messages",
            theme.embed(
                title="🧹 Messages bulk deleted",
                description=f"**{len(messages)}** messages removed from {messages[0].channel.mention}.",
                color=theme.LOSE,
                timestamp=True,
            ),
        )

    # -- structure ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        actor = await self._actor(channel.guild, discord.AuditLogAction.channel_create, channel.id)
        await self.bot.audit(
            channel.guild,
            "channels",
            theme.embed(
                title="📁 Channel created",
                description=f"**{channel.name}** (`{channel.id}`)" + (f" by {actor.mention}" if actor else ""),
                color=theme.WIN,
                timestamp=True,
            ),
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        actor = await self._actor(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
        await self.bot.audit(
            channel.guild,
            "channels",
            theme.embed(
                title="🗑️ Channel deleted",
                description=f"**{channel.name}** (`{channel.id}`)" + (f" by {actor.mention}" if actor else ""),
                color=theme.LOSE,
                timestamp=True,
            ),
        )
        # A deleted ticket channel should not stay in the ticket table.
        await self.bot.tickets.delete(channel.id)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self.bot.audit(
            role.guild,
            "roles",
            theme.embed(title="🆕 Role created", description=f"{role.mention} (`{role.id}`)", color=theme.WIN, timestamp=True),
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self.bot.audit(
            role.guild,
            "roles",
            theme.embed(title="🗑️ Role deleted", description=f"**{role.name}** (`{role.id}`)", color=theme.LOSE, timestamp=True),
        )

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        changes = []
        if before.name != after.name:
            changes.append(f"**Name** {before.name} → {after.name}")
        if before.owner_id != after.owner_id:
            changes.append(f"**Owner** <@{before.owner_id}> → <@{after.owner_id}>")
        if changes:
            await self.bot.audit(
                after,
                "server",
                theme.embed(title="🏰 Server updated", description="\n".join(changes), color=theme.INFO, timestamp=True),
            )

    # -- moderation events -------------------------------------------------

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.abc.User) -> None:
        actor = await self._actor(guild, discord.AuditLogAction.ban, user.id)
        await self.bot.audit(
            guild,
            "moderation",
            theme.embed(
                title="🔨 Member banned",
                description=f"**{user}** (`{user.id}`)" + (f" by {actor.mention}" if actor else ""),
                color=theme.LOSE,
                timestamp=True,
            ),
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.abc.User) -> None:
        actor = await self._actor(guild, discord.AuditLogAction.unban, user.id)
        await self.bot.audit(
            guild,
            "moderation",
            theme.embed(
                title="✅ Member unbanned",
                description=f"**{user}** (`{user.id}`)" + (f" by {actor.mention}" if actor else ""),
                color=theme.WIN,
                timestamp=True,
            ),
        )


async def setup(bot: Any) -> None:
    await bot.add_cog(ServerLog(bot))
