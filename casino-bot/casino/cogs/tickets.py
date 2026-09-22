"""Support tickets: a private channel per request, with transcripts.

Kept from the old bot because it worked, with three changes: the panel view
is actually registered as persistent (so its buttons still work after a
restart), closing a ticket no longer silently removes the owner's view of
their own channel, and deleting one always writes a transcript first if a
transcript channel is configured.
"""

from __future__ import annotations

import io
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from casino.core.checks import is_staff, staff_only
from casino.core.errors import CasinoError, NotPermitted
from casino.ui import theme
from casino.ui.responder import Responder

CATEGORIES = {
    "general": ("General support", "🎫", "Questions and anything else"),
    "economy": ("Economy issue", "💰", "Balances, payouts, transfers"),
    "casino": ("Casino issue", "🎰", "A game behaved unexpectedly"),
    "report": ("Report a player", "🚨", "Escalate someone to staff"),
    "appeal": ("Appeal", "⚖️", "Ask staff to review a punishment"),
}


class Tickets(commands.Cog, name="Tickets"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @commands.hybrid_command(name="ticketsetup", description="Create the ticket category, staff role and panel.")
    @app_commands.describe(
        category="Where ticket channels are created.",
        support_role="The role that can see and manage tickets.",
        transcripts="Where transcripts are posted when a ticket is deleted.",
    )
    @staff_only()
    async def ticketsetup(
        self,
        ctx: commands.Context,
        category: discord.CategoryChannel | None = None,
        support_role: discord.Role | None = None,
        transcripts: discord.TextChannel | None = None,
    ) -> None:
        guild = ctx.guild
        if not guild.me.guild_permissions.manage_channels:
            raise CasinoError("I need **Manage Channels** to set tickets up.")

        created: list[str] = []
        if category is None:
            category = discord.utils.get(guild.categories, name="Tickets")
            if category is None:
                category = await guild.create_category("Tickets", reason="Ticket setup")
                created.append("category")
        if support_role is None:
            support_role = discord.utils.get(guild.roles, name="Support")
            if support_role is None:
                if not guild.me.guild_permissions.manage_roles:
                    raise CasinoError("I need **Manage Roles** to create the default Support role.")
                support_role = await guild.create_role(name="Support", reason="Ticket setup")
                created.append("Support role")
        if transcripts is None:
            transcripts = discord.utils.get(guild.text_channels, name="ticket-transcripts")
            if transcripts is None:
                transcripts = await guild.create_text_channel(
                    "ticket-transcripts",
                    category=category,
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        support_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                    },
                    reason="Ticket setup",
                )
                created.append("transcript channel")

        await self.bot.guilds_store.update(
            guild.id,
            ticket_category=category.id,
            ticket_support_role=support_role.id,
            ticket_transcript_channel=transcripts.id,
        )

        embed = theme.embed(
            title=f"{theme.ICON['ticket']} Tickets are ready",
            description="A panel has been posted below. Pin it wherever members should open tickets.",
            color=theme.WIN,
            footer="Created: " + ", ".join(created) if created else "Used your existing channels and roles",
        )
        embed.add_field(name="Category", value=category.mention, inline=True)
        embed.add_field(name="Support role", value=support_role.mention, inline=True)
        embed.add_field(name="Transcripts", value=transcripts.mention, inline=True)
        await Responder(ctx).send(embed=embed)
        await ctx.channel.send(embed=panel_embed(), view=TicketPanelView(self.bot))

    @commands.hybrid_command(name="ticketpanel", description="Post the ticket panel in this channel.")
    @staff_only()
    async def ticketpanel(self, ctx: commands.Context) -> None:
        settings = await self.bot.guilds_store.settings(ctx.guild.id)
        if not settings.get("ticket_category"):
            raise CasinoError("Run `/ticketsetup` first.")
        await ctx.channel.send(embed=panel_embed(), view=TicketPanelView(self.bot))
        if ctx.interaction is not None:
            await ctx.send(embed=theme.success("Panel posted."), ephemeral=True)

    @commands.hybrid_command(name="ticketadd", description="Add a member to this ticket.")
    @staff_only()
    async def ticketadd(self, ctx: commands.Context, member: discord.Member) -> None:
        ticket = await self.bot.tickets.get(ctx.channel.id)
        if ticket is None:
            raise CasinoError("This channel is not a ticket.")
        await ctx.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
        await Responder(ctx).send(embed=theme.success(f"Added {member.mention} to this ticket."))


def panel_embed() -> discord.Embed:
    return theme.embed(
        title=f"{theme.ICON['ticket']} Need a hand?",
        description=(
            "Pick the closest category below and a private channel will open with staff.\n\n"
            + "\n".join(f"{emoji} **{label}** — {blurb}" for label, emoji, blurb in CATEGORIES.values())
        ),
        color=theme.BRAND,
        footer="One open ticket per person at a time.",
    )


async def open_ticket(bot: Any, interaction: discord.Interaction, category_key: str) -> None:
    """Create the private channel, or point at the one already open."""
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message(
            embed=theme.error("Tickets only work inside a server."), ephemeral=True
        )

    existing = await bot.tickets.open_for(guild.id, interaction.user.id)
    if existing:
        channel = guild.get_channel(int(existing))
        if channel is not None:
            return await interaction.response.send_message(
                embed=theme.error(f"You already have a ticket open: {channel.mention}"), ephemeral=True
            )
        await bot.tickets.delete(int(existing))  # channel was deleted by hand

    settings = await bot.guilds_store.settings(guild.id)
    category = guild.get_channel(int(settings["ticket_category"])) if settings.get("ticket_category") else None
    support_role = guild.get_role(int(settings["ticket_support_role"])) if settings.get("ticket_support_role") else None

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True, read_message_history=True
        ),
    }
    if support_role is not None:
        overwrites[support_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )

    label, emoji, _ = CATEGORIES.get(category_key, CATEGORIES["general"])
    try:
        channel = await guild.create_text_channel(
            f"{category_key}-{interaction.user.name[:16].lower()}",
            category=category if isinstance(category, discord.CategoryChannel) else None,
            overwrites=overwrites,
            topic=f"{label} · opened by {interaction.user} ({interaction.user.id})",
            reason=f"Ticket opened by {interaction.user}",
        )
    except discord.Forbidden:
        return await interaction.response.send_message(
            embed=theme.error("I need **Manage Channels**, and access to the ticket category."),
            ephemeral=True,
        )

    await bot.tickets.create(guild.id, channel.id, interaction.user.id, category_key)
    await interaction.response.send_message(
        embed=theme.success(f"Opened {channel.mention}."), ephemeral=True
    )

    embed = theme.embed(
        title=f"{emoji} {label}",
        description=settings.get("ticket_message") or "A staff member will be with you shortly.",
        color=theme.BRAND,
        timestamp=True,
    )
    embed.add_field(name="Opened by", value=interaction.user.mention, inline=True)
    embed.add_field(name="Category", value=label, inline=True)
    embed.set_footer(text="Staff can claim, close, reopen or delete this ticket with the buttons below.")
    await channel.send(
        content=support_role.mention if support_role else None,
        embed=embed,
        view=TicketControlView(bot),
    )


class TicketPanelView(discord.ui.View):
    """Persistent panel. Registered at startup so it never goes stale."""

    def __init__(self, bot: Any) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        select: discord.ui.Select = discord.ui.Select(
            placeholder="Open a ticket…",
            custom_id="casino:ticket:category",
            options=[
                discord.SelectOption(label=label, value=key, emoji=emoji, description=blurb)
                for key, (label, emoji, blurb) in CATEGORIES.items()
            ],
        )
        select.callback = self._selected  # type: ignore[method-assign]
        self.select = select
        self.add_item(select)

    async def _selected(self, interaction: discord.Interaction) -> None:
        await open_ticket(self.bot, interaction, self.select.values[0])


class TicketControlView(discord.ui.View):
    """Persistent controls inside each ticket channel."""

    def __init__(self, bot: Any) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    async def _ticket(self, interaction: discord.Interaction) -> dict[str, Any]:
        ticket = await self.bot.tickets.get(interaction.channel.id)
        if ticket is None:
            raise CasinoError("This channel is not a tracked ticket.")
        return ticket

    async def _require_staff(self, interaction: discord.Interaction) -> None:
        if not await is_staff(self.bot, interaction.user):
            raise NotPermitted("Only ticket staff can do that.")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        message = getattr(error, "message", None) or "Something went wrong."
        embed = theme.error(message) if isinstance(error, CasinoError) else theme.error(
            "Something went wrong handling that button."
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Claim", emoji="👤", style=discord.ButtonStyle.success, custom_id="casino:ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._ticket(interaction)
        await self._require_staff(interaction)
        await self.bot.tickets.claim(interaction.channel.id, interaction.user.id)
        await interaction.response.send_message(
            embed=theme.embed(
                description=f"👤 {interaction.user.mention} is handling this ticket.", color=theme.INFO
            )
        )

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="casino:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = await self._ticket(interaction)
        staff = await is_staff(self.bot, interaction.user)
        if not staff and interaction.user.id != int(ticket["user_id"]):
            raise NotPermitted("Only the person who opened this ticket, or staff, can close it.")

        owner = interaction.guild.get_member(int(ticket["user_id"]))
        if owner is not None:
            # Keep read access: the owner should still be able to see the
            # conversation, they just cannot add to it.
            await interaction.channel.set_permissions(
                owner, view_channel=True, send_messages=False, read_message_history=True
            )
        await self.bot.tickets.set_closed(interaction.channel.id, True)
        await interaction.response.send_message(
            embed=theme.embed(
                title="🔒 Ticket closed",
                description="Staff can reopen or delete it with the buttons above.",
                color=theme.WARN,
            )
        )

    @discord.ui.button(label="Reopen", emoji="🔓", style=discord.ButtonStyle.primary, custom_id="casino:ticket:reopen")
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = await self._ticket(interaction)
        await self._require_staff(interaction)
        owner = interaction.guild.get_member(int(ticket["user_id"]))
        if owner is not None:
            await interaction.channel.set_permissions(
                owner, view_channel=True, send_messages=True, read_message_history=True
            )
        await self.bot.tickets.set_closed(interaction.channel.id, False)
        await interaction.response.send_message(embed=theme.success("Ticket reopened."))

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="casino:ticket:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        ticket = await self._ticket(interaction)
        await self._require_staff(interaction)
        settings = await self.bot.guilds_store.settings(interaction.guild.id)
        await interaction.response.send_message(
            embed=theme.embed(description="Writing the transcript, then deleting…", color=theme.WARN),
            ephemeral=True,
        )

        channel_id = settings.get("ticket_transcript_channel")
        target = interaction.guild.get_channel(int(channel_id)) if channel_id else None
        if isinstance(target, discord.TextChannel):
            lines = []
            try:
                async for message in interaction.channel.history(limit=1000, oldest_first=True):
                    body = message.content or ""
                    if message.embeds:
                        body += " [embed]"
                    if message.attachments:
                        body += " " + " ".join(a.url for a in message.attachments)
                    lines.append(f"[{message.created_at:%Y-%m-%d %H:%M}] {message.author}: {body}".rstrip())
            except discord.HTTPException:
                pass
            transcript = "\n".join(lines) or "No messages."
            await target.send(
                embed=theme.embed(
                    title=f"📄 Transcript · #{interaction.channel.name}",
                    description=(
                        f"Opened by <@{ticket['user_id']}> · "
                        f"{'claimed by <@%s>' % ticket['claimed_by'] if ticket.get('claimed_by') else 'unclaimed'} · "
                        f"closed by {interaction.user.mention}"
                    ),
                    color=theme.INFO,
                    timestamp=True,
                ),
                file=discord.File(
                    io.BytesIO(transcript.encode("utf-8", errors="replace")),
                    filename=f"{interaction.channel.name}.txt",
                ),
            )

        await self.bot.tickets.delete(interaction.channel.id)
        await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user}")


async def setup(bot: Any) -> None:
    await bot.add_cog(Tickets(bot))
