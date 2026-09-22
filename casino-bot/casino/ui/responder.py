"""One way to reply, whether the command arrived as a slash or a prefix.

Discord gives you two very different objects: a ``Context`` for text commands
and an ``Interaction`` for slash commands, buttons and modals. The old bot
solved this by writing each game twice — ``roulette`` for text and
``play_roulette_ui`` for buttons — which is why the two copies had different
validation and different embeds.

:class:`Responder` hides the difference, so a game is written once and reached
from anywhere.
"""

from __future__ import annotations

from typing import Any

import discord
from discord.ext import commands

Source = commands.Context | discord.Interaction


class Responder:
    def __init__(self, source: Source) -> None:
        self.source = source
        self.message: discord.Message | discord.InteractionMessage | None = None

    # -- identity ----------------------------------------------------------

    @property
    def is_interaction(self) -> bool:
        return isinstance(self.source, discord.Interaction)

    @property
    def interaction(self) -> discord.Interaction | None:
        if isinstance(self.source, discord.Interaction):
            return self.source
        return self.source.interaction

    @property
    def user(self) -> discord.abc.User:
        return self.source.user if isinstance(self.source, discord.Interaction) else self.source.author

    @property
    def guild(self) -> discord.Guild | None:
        return self.source.guild

    @property
    def guild_id(self) -> int | None:
        return self.source.guild.id if self.source.guild else None

    @property
    def channel(self) -> Any:
        return self.source.channel

    @property
    def client(self) -> Any:
        return self.source.client if isinstance(self.source, discord.Interaction) else self.source.bot

    # -- sending -----------------------------------------------------------

    async def send(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        ephemeral: bool = False,
        files: list[discord.File] | None = None,
    ) -> discord.Message | discord.InteractionMessage | None:
        """Send the first (or next) reply and remember it for later edits."""
        kwargs: dict[str, Any] = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        if files:
            kwargs["files"] = files

        if isinstance(self.source, discord.Interaction):
            if self.source.response.is_done():
                self.message = await self.source.followup.send(**kwargs, ephemeral=ephemeral, wait=True)
            else:
                await self.source.response.send_message(**kwargs, ephemeral=ephemeral)
                self.message = await self.source.original_response()
        else:
            if self.source.interaction is not None:
                self.message = await self.source.send(**kwargs, ephemeral=ephemeral)
            else:
                self.message = await self.source.send(**kwargs)

        if view is not None and hasattr(view, "message"):
            view.message = self.message
        return self.message

    async def edit(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        clear_view: bool = False,
    ) -> None:
        """Edit the message this responder already sent."""
        if self.message is None:
            await self.send(content=content, embed=embed, view=view)
            return
        kwargs: dict[str, Any] = {}
        if content is not None or embed is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        elif clear_view:
            kwargs["view"] = None
        try:
            await self.message.edit(**kwargs)
        except (discord.NotFound, discord.HTTPException):
            pass
        if view is not None and hasattr(view, "message"):
            view.message = self.message

    async def defer(self, *, ephemeral: bool = False) -> None:
        """Buy time on an interaction. A no-op for prefix commands, which
        have no three-second deadline to worry about."""
        if isinstance(self.source, discord.Interaction) and not self.source.response.is_done():
            await self.source.response.defer(ephemeral=ephemeral)
