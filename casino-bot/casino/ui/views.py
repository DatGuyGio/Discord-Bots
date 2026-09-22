"""Reusable interaction components.

Three things every view here gets for free, and none of which the old views
had:

* **Ownership.** A view created for one player refuses clicks from everyone
  else, with an explanation. Previously any passer-by could press *Claim
  Daily* on someone else's challenge card and have it apply to themselves.
* **Error rendering.** A :class:`~casino.core.errors.CasinoError` raised
  inside a callback becomes a tidy ephemeral embed instead of Discord's
  "This interaction failed".
* **Expiry.** On timeout the buttons visibly disable, so a stale message
  cannot be clicked into a confusing error.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Sequence

import discord

from casino.core.errors import CasinoError
from casino.ui import theme

log = logging.getLogger(__name__)


class CasinoView(discord.ui.View):
    """Base class for every non-persistent view."""

    def __init__(self, *, owner_id: int | None = None, timeout: float | None = 180) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.message: discord.Message | discord.InteractionMessage | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is None or interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            embed=theme.error("These buttons belong to someone else. Run the command yourself to play."),
            ephemeral=True,
        )
        return False

    def disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        if isinstance(error, CasinoError):
            embed = theme.embed(
                title=f"{theme.ICON['warn']} {error.title}",
                description=error.message,
                color=theme.WARN,
            )
        else:
            log.exception("component error on %r", item, exc_info=error)
            embed = theme.error("Something broke handling that click. The bot log has the details.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


class Paginator(CasinoView):
    """Page through a list of embeds.

    Leaderboards and transaction history used to dump a single fixed block of
    ten rows with no way to see row eleven.
    """

    def __init__(self, pages: Sequence[discord.Embed], *, owner_id: int | None = None) -> None:
        super().__init__(owner_id=owner_id, timeout=240)
        self.pages = list(pages) or [theme.embed(description="Nothing to show.")]
        self.index = 0
        self._sync()

    def _sync(self) -> None:
        single = len(self.pages) <= 1
        self.first.disabled = self.previous.disabled = self.index == 0 or single
        self.next.disabled = self.last.disabled = self.index >= len(self.pages) - 1 or single
        self.counter.label = f"{self.index + 1} / {len(self.pages)}"

    @property
    def current(self) -> discord.Embed:
        return self.pages[self.index]

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.current, view=self)

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = 0
        await self._show(interaction)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._show(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index = len(self.pages) - 1
        await self._show(interaction)


class Confirm(CasinoView):
    """A deliberate two-step for destructive actions."""

    def __init__(
        self,
        *,
        owner_id: int,
        confirm_label: str = "Confirm",
        confirm_emoji: str | None = "⚠️",
        danger: bool = True,
    ) -> None:
        super().__init__(owner_id=owner_id, timeout=60)
        self.value: bool | None = None
        self.confirm.label = confirm_label
        self.confirm.emoji = discord.PartialEmoji.from_str(confirm_emoji) if confirm_emoji else None
        self.confirm.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = True
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = False
        self.disable_all()
        await interaction.response.edit_message(
            embed=theme.embed(description="Cancelled. Nothing was changed.", color=theme.PUSH),
            view=self,
        )
        self.stop()


class AmountModal(discord.ui.Modal):
    """A stake prompt. ``all``, ``half``, ``10k`` and ``25%`` all work."""

    def __init__(
        self,
        title: str,
        handler: Callable[[discord.Interaction, str], Awaitable[Any]],
        *,
        label: str = "Stake",
        placeholder: str = "e.g. 500, 10k, half, all",
        default: str | None = None,
    ) -> None:
        super().__init__(title=title[:45], timeout=300)
        self.handler = handler
        self.amount: discord.ui.TextInput = discord.ui.TextInput(
            label=label, placeholder=placeholder, default=default, max_length=20, required=True
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.handler(interaction, str(self.amount.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        if isinstance(error, CasinoError):
            embed = theme.embed(
                title=f"{theme.ICON['warn']} {error.title}", description=error.message, color=theme.WARN
            )
        else:
            log.exception("modal error", exc_info=error)
            embed = theme.error("Something broke handling that. The bot log has the details.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def send_view(
    interaction: discord.Interaction, embed: discord.Embed, view: CasinoView, *, ephemeral: bool = False
) -> None:
    """Send a view and remember the message so timeouts can disable it."""
    if interaction.response.is_done():
        view.message = await interaction.followup.send(embed=embed, view=view, ephemeral=ephemeral, wait=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=ephemeral)
        view.message = await interaction.original_response()
