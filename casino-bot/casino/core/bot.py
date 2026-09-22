"""The bot object: wiring, lifecycle, and one place where errors are rendered.

Structural notes:

* Features are cogs. The old bot was one 3,000-line module where a helper
  defined on line 2,700 was called on line 300 and only worked because Python
  resolves globals late; reordering two blocks could break it at runtime.
* Storage is composed here and handed to the cogs, so a cog never reaches
  into another cog's state (the old challenge UI reached into ``bank._lock``
  and ``bank._data`` directly from a button callback).
* Every command is a *hybrid* command: one implementation, reachable as
  ``/balance`` and as ``!balance``. Slash commands give the discoverability
  and validation the old bot lacked; keeping the prefix means nobody has to
  relearn anything.
"""

from __future__ import annotations

import logging
import sys
import traceback
from typing import Any

import discord
from discord.ext import commands

from casino.config import Settings, settings as default_settings
from casino.core.errors import CasinoError
from casino.core.guard import RoundGuard
from casino.db.database import Database
from casino.db.economy import Economy
from casino.db.guilds import GuildStore
from casino.db.progression import Progression
from casino.db.support import Moderation, Tickets
from casino.ui import theme

log = logging.getLogger("casino")

COGS = (
    "casino.cogs.hub",
    "casino.cogs.economy",
    "casino.cogs.games",
    "casino.cogs.blackjack",
    "casino.cogs.progression",
    "casino.cogs.leaderboards",
    "casino.cogs.moderation",
    "casino.cogs.tickets",
    "casino.cogs.serverlog",
    "casino.cogs.admin",
    "casino.cogs.meta",
)


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.message_content = True  # prefix commands
    intents.members = True  # welcome/goodbye, role logs, nickname logs
    return intents


class CasinoBot(commands.Bot):
    def __init__(self, config: Settings | None = None, **kwargs: Any) -> None:
        self.config = config or default_settings
        super().__init__(
            command_prefix=commands.when_mentioned_or(self.config.prefix),
            intents=build_intents(),
            help_command=None,
            case_insensitive=True,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            activity=discord.Activity(type=discord.ActivityType.playing, name="/casino"),
            **kwargs,
        )
        self.db = Database(self.config.database_path)
        self.economy: Economy
        self.guilds_store: GuildStore
        self.progression: Progression
        self.moderation: Moderation
        self.tickets: Tickets
        self.guard: RoundGuard

    # -- lifecycle ---------------------------------------------------------

    async def prepare(self) -> None:
        """Everything needed to run except talking to Discord.

        Split out from :meth:`setup_hook` so the test suite can build a fully
        wired bot — storage, cogs, persistent views — without a token or a
        network connection. ``tests/test_bot.py`` calls this to prove that
        every cog imports and every command name is one Discord will accept.
        """
        await self.db.connect()
        self.economy = Economy(self.db)
        self.guilds_store = GuildStore(self.db)
        self.progression = Progression(self.db)
        self.moderation = Moderation(self.db)
        self.tickets = Tickets(self.db)
        self.guard = RoundGuard(self.economy, self.guilds_store, self.progression)

        if self.config.owner_ids:
            self.owner_ids = set(self.config.owner_ids)

        for module in COGS:
            try:
                await self.load_extension(module)
                log.debug("loaded %s", module)
            except Exception:
                log.exception("failed to load %s", module)

        # Persistent components: views with timeout=None and stable custom_ids
        # keep working after a restart, which is why the casino hub and the
        # ticket panel can be posted once and pinned forever.
        from casino.ui.hub import HubView
        from casino.cogs.tickets import TicketPanelView, TicketControlView

        self.add_view(HubView(self))
        self.add_view(TicketPanelView(self))
        self.add_view(TicketControlView(self))

    async def setup_hook(self) -> None:
        await self.prepare()
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("slash commands synced to dev guild %s", self.config.dev_guild_id)
        else:
            await self.tree.sync()
            log.info("slash commands synced globally")

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s) in %d guild(s)", self.user, self.user.id, len(self.guilds))

    # -- shared helpers ----------------------------------------------------

    async def settings_for(self, guild: discord.Guild | None) -> dict[str, Any]:
        return await self.guilds_store.settings(guild.id if guild else None)

    async def log_channel(self, guild: discord.Guild) -> discord.abc.Messageable | None:
        settings = await self.guilds_store.settings(guild.id)
        channel_id = settings.get("logs_channel")
        if not channel_id:
            return None
        channel = guild.get_channel(int(channel_id))
        return channel if isinstance(channel, discord.TextChannel) else None

    async def audit(
        self,
        guild: discord.Guild | None,
        kind: str,
        embed: discord.Embed,
    ) -> None:
        """Send an embed to the guild's log channel if that category is on."""
        if guild is None:
            return
        if not await self.guilds_store.log_enabled(guild.id, kind):
            return
        channel = await self.log_channel(guild)
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            log.debug("could not write to the log channel in guild %s", guild.id)

    # -- error handling ----------------------------------------------------

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        error = getattr(error, "original", error) if isinstance(error, commands.CommandInvokeError) else error

        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, CasinoError):
            return await self._reply(ctx, theme.embed(
                title=f"{theme.ICON['warn']} {error.title}",
                description=error.message,
                color=theme.WARN,
            ), ephemeral=error.ephemeral)
        if isinstance(error, commands.MissingRequiredArgument):
            usage = f"{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}".strip()
            return await self._reply(ctx, theme.error(
                f"`{error.param.name}` is missing.\n\n**Usage**\n`{usage}`"
            ))
        if isinstance(error, (commands.BadArgument, commands.BadUnionArgument)):
            return await self._reply(ctx, theme.error(
                "I could not read one of those arguments. Mention users and channels "
                "directly (`@name`, `#channel`), or use the slash-command version, "
                "which validates as you type."
            ))
        if isinstance(error, commands.NoPrivateMessage):
            return await self._reply(ctx, theme.error("That command only works inside a server."))
        if isinstance(error, commands.CheckFailure):
            return await self._reply(ctx, theme.denied("You do not have permission to use that."))
        if isinstance(error, commands.CommandOnCooldown):
            return await self._reply(ctx, theme.error(f"Try again in **{error.retry_after:.1f}s**."))
        if isinstance(error, commands.MaxConcurrencyReached):
            return await self._reply(ctx, theme.error("That command is already running for you."))
        if isinstance(error, commands.BotMissingPermissions):
            missing = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            return await self._reply(ctx, theme.error(f"I am missing these Discord permissions: **{missing}**."))

        log.error("unhandled error in %s", ctx.command, exc_info=error)
        await self._reply(ctx, theme.error(
            "Something broke while running that. The details are in the bot log."
        ))

    async def _reply(self, ctx: commands.Context, embed: discord.Embed, ephemeral: bool = True) -> None:
        try:
            await ctx.send(embed=embed, ephemeral=ephemeral)
        except (discord.HTTPException, TypeError):
            try:
                await ctx.send(embed=embed)
            except discord.HTTPException:
                pass

    async def on_error(self, event: str, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        log.error("error in event %s", event)
        traceback.print_exc(file=sys.stderr)
