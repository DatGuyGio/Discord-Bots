"""Help and utility commands.

The old ``!help`` was a single wall of text pasted into one embed, which
Discord truncates at 4,096 characters — so the bottom of the list was simply
invisible. This one is built from the command tree itself, split by category
behind a dropdown, so it cannot go out of date or run out of room.
"""

from __future__ import annotations

import platform
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from casino.ui import theme
from casino.ui.responder import Responder
from casino.ui.views import CasinoView

CATEGORY_EMOJI = {
    "Hub": "🎰",
    "Games": "🎲",
    "Blackjack": "🃏",
    "Economy": "💰",
    "Progression": "⭐",
    "Leaderboards": "🏆",
    "Moderation": "🛡️",
    "Tickets": "🎫",
    "Admin": "⚙️",
    "Server log": "📜",
    "Meta": "ℹ️",
}


class Meta(commands.Cog, name="Meta"):
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # help
    # ------------------------------------------------------------------

    def _commands_for(self, cog_name: str) -> list[commands.Command]:
        cog = self.bot.get_cog(cog_name)
        if cog is None:
            return []
        seen: dict[str, commands.Command] = {}
        for command in cog.walk_commands():
            if command.hidden:
                continue
            seen[command.qualified_name] = command
        return sorted(seen.values(), key=lambda c: c.qualified_name)

    def category_embed(self, cog_name: str, prefix: str) -> discord.Embed:
        commands_list = self._commands_for(cog_name)
        lines = []
        for command in commands_list:
            signature = f" {command.signature}" if command.signature else ""
            description = command.description or command.short_doc or ""
            lines.append(f"**`/{command.qualified_name}`**{signature}\n　{description}")
        cog = self.bot.get_cog(cog_name)
        return theme.embed(
            title=f"{CATEGORY_EMOJI.get(cog_name, '•')} {cog_name}",
            description=(cog.description or "").strip() + "\n\n" + "\n".join(lines) if lines else "Nothing here.",
            color=theme.BRAND,
            footer=f"Every command also works as {prefix}{commands_list[0].qualified_name if commands_list else 'command'}",
        )

    def overview_embed(self, prefix: str) -> discord.Embed:
        embed = theme.embed(
            title="🎰 Casino bot · command guide",
            description=(
                "Everything works as a slash command **and** with the "
                f"`{prefix}` prefix. Slash is easier: Discord shows the options "
                "and validates them as you type.\n\n"
                "Pick a category below for the full list."
            ),
            color=theme.BRAND,
        )
        embed.add_field(
            name="Start here",
            value=theme.bullet_list(
                [
                    "`/casino` — the table menu",
                    "`/daily` and `/work` — free chips",
                    "`/blackjack 500` — the best odds in the house",
                    "`/mines 500 3` — click tiles, bank the multiplier",
                    "`/crash 500` — jump off before it pops",
                ]
            ),
            inline=False,
        )
        embed.add_field(
            name="Worth knowing",
            value=theme.bullet_list(
                [
                    "Stakes accept `all`, `half`, `25%`, `10k`, `2.5m`",
                    "`/verify` proves any round was not rigged",
                    "`/wagerlimit` and `/selfexclude` are yours to use, and staff cannot undo them",
                    "Chips are virtual and separate in every server",
                ]
            ),
            inline=False,
        )
        return embed

    @commands.hybrid_command(name="help", description="Every command, grouped by category.")
    @app_commands.describe(command="Get detail on one command instead.")
    async def help_command(self, ctx: commands.Context, command: str | None = None) -> None:
        responder = Responder(ctx)
        prefix = self.bot.config.prefix

        if command:
            found = self.bot.get_command(command.lstrip("/"))
            if found is None:
                return await responder.send(
                    embed=theme.error(f"There is no `{command}` command. Try `/help`."), ephemeral=True
                )
            signature = f" {found.signature}" if found.signature else ""
            embed = theme.embed(
                title=f"/{found.qualified_name}",
                description=found.description or found.help or "No description.",
                color=theme.BRAND,
            )
            embed.add_field(name="Usage", value=f"`/{found.qualified_name}{signature}`", inline=False)
            if found.aliases:
                embed.add_field(
                    name="Also known as",
                    value=" ".join(f"`{prefix}{alias}`" for alias in found.aliases),
                    inline=False,
                )
            return await responder.send(embed=embed)

        view = HelpView(self, prefix)
        await responder.send(embed=self.overview_embed(prefix), view=view)

    # ------------------------------------------------------------------
    # utility
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="ping", description="Check the bot's latency.")
    async def ping(self, ctx: commands.Context) -> None:
        gateway = round(self.bot.latency * 1000)
        started = time.perf_counter()
        await self.bot.db.fetchval("SELECT 1")
        database = (time.perf_counter() - started) * 1000
        await Responder(ctx).send(
            embed=theme.embed(
                title="🏓 Pong",
                description=f"**Gateway** {gateway} ms\n**Database** {database:.1f} ms",
                color=theme.INFO,
            )
        )

    @commands.hybrid_command(name="userinfo", aliases=["whois"], description="Details about a member.")
    async def userinfo(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        member = member or ctx.author
        roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
        embed = theme.embed(
            title=f"👤 {member.display_name}",
            color=member.color if member.color.value else theme.BRAND,
            thumbnail=member.display_avatar.url,
        )
        embed.add_field(name="Username", value=str(member), inline=True)
        embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Bot", value="yes" if member.bot else "no", inline=True)
        embed.add_field(
            name="Joined",
            value=theme.relative(member.joined_at) if member.joined_at else "unknown",
            inline=True,
        )
        embed.add_field(name="Registered", value=theme.relative(member.created_at), inline=True)
        embed.add_field(name="Top role", value=member.top_role.mention, inline=True)
        if roles:
            embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles)[:1024], inline=False)
        if member.is_timed_out():
            embed.add_field(
                name="Timed out until", value=theme.relative(member.timed_out_until), inline=False
            )
        await Responder(ctx).send(embed=embed)

    @commands.hybrid_command(name="serverinfo", aliases=["guildinfo"], description="Details about this server.")
    async def serverinfo(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        humans = sum(1 for member in guild.members if not member.bot)
        embed = theme.embed(
            title=f"🏰 {guild.name}",
            color=theme.BRAND,
            thumbnail=guild.icon.url if guild.icon else None,
        )
        embed.add_field(name="Members", value=f"{guild.member_count:,} ({humans:,} human)", inline=True)
        embed.add_field(name="Created", value=theme.relative(guild.created_at), inline=True)
        embed.add_field(name="Owner", value=guild.owner.mention if guild.owner else "unknown", inline=True)
        embed.add_field(
            name="Channels",
            value=f"{len(guild.text_channels)} text · {len(guild.voice_channels)} voice",
            inline=True,
        )
        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(name="Boosts", value=f"level {guild.premium_tier} · {guild.premium_subscription_count}", inline=True)
        totals = await self.bot.economy.guild_totals(guild.id)
        embed.add_field(
            name="Casino",
            value=(
                f"{int(totals.get('players', 0)):,} accounts · "
                f"{int(totals.get('games', 0)):,} rounds · "
                f"{int(totals.get('chips', 0)):,} chips in circulation"
            ),
            inline=False,
        )
        await Responder(ctx).send(embed=embed)

    @commands.hybrid_command(name="avatar", aliases=["pfp"], description="Show a member's avatar.")
    async def avatar(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        member = member or ctx.author
        embed = theme.embed(title=f"🖼️ {member.display_name}", color=theme.BRAND)
        embed.set_image(url=member.display_avatar.url)
        await Responder(ctx).send(embed=embed)

    @commands.hybrid_command(name="about", description="What this bot is and how it works.")
    async def about(self, ctx: commands.Context) -> None:
        embed = theme.embed(
            title="🎰 About this bot",
            description=(
                "A casino, economy and moderation bot. The chips are virtual, scoped "
                "per server, and cannot be bought with real money.\n\n"
                "Every wager is a single database transaction, so a balance can never "
                "go negative and two clicks can never spend the same chips twice. "
                "Every round is generated from a committed seed you can verify with "
                "`/verify`."
            ),
            color=theme.BRAND,
        )
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="discord.py", value=discord.__version__, inline=True)
        embed.add_field(name="Python", value=platform.python_version(), inline=True)
        embed.set_footer(text="If gambling stops being fun, /selfexclude locks you out and nobody can undo it.")
        await Responder(ctx).send(embed=embed)

    @commands.command(name="sync", hidden=True)
    @commands.is_owner()
    async def sync(self, ctx: commands.Context, scope: str = "guild") -> None:
        """Push slash commands to Discord. Guild scope is instant; global is not."""
        if scope == "global":
            synced = await self.bot.tree.sync()
            return await ctx.send(embed=theme.success(f"Synced **{len(synced)}** commands globally."))
        self.bot.tree.copy_global_to(guild=ctx.guild)
        synced = await self.bot.tree.sync(guild=ctx.guild)
        await ctx.send(embed=theme.success(f"Synced **{len(synced)}** commands to this server."))


class HelpView(CasinoView):
    def __init__(self, cog: Meta, prefix: str) -> None:
        super().__init__(owner_id=None, timeout=300)
        self.cog = cog
        self.prefix = prefix
        options = [discord.SelectOption(label="Overview", value="__overview__", emoji="🏠")]
        for name in CATEGORY_EMOJI:
            if cog.bot.get_cog(name) is None:
                continue
            count = len(cog._commands_for(name))
            if not count:
                continue
            options.append(
                discord.SelectOption(
                    label=name, value=name, emoji=CATEGORY_EMOJI[name], description=f"{count} command(s)"
                )
            )
        select: discord.ui.Select = discord.ui.Select(placeholder="Choose a category…", options=options)
        select.callback = self._selected  # type: ignore[method-assign]
        self.select = select
        self.add_item(select)

    async def _selected(self, interaction: discord.Interaction) -> None:
        choice = self.select.values[0]
        embed = (
            self.cog.overview_embed(self.prefix)
            if choice == "__overview__"
            else self.cog.category_embed(choice, self.prefix)
        )
        await interaction.response.edit_message(embed=embed, view=self)


async def setup(bot: Any) -> None:
    await bot.add_cog(Meta(bot))
