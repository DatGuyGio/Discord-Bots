"""The casino hub: one pinned message that is the whole front end.

This replaces the old ``!casino`` dashboard. Four things changed.

*It survives restarts properly.* The old view used ``timeout=None`` and
stable custom ids but was never registered with ``bot.add_view``, so after a
restart every button on the pinned message failed silently. This one is
registered in :meth:`casino.core.bot.CasinoBot.setup_hook`.

*It shows live state.* Balance, level progress, streak, and cooldowns are
read when the panel is opened rather than being static text.

*It routes into the same code paths as the slash commands*, so a bet placed
from a button gets exactly the same validation as a typed one.

*Sub-screens are ephemeral and have a way back*, so checking your balance
does not leave a trail of messages in the channel.
"""

from __future__ import annotations

from typing import Any

import discord

from casino.config import GAME_KEYS
from casino.db.economy import parse_iso
from casino.db.progression import xp_for_level
from casino.ui import theme
from casino.ui.views import AmountModal

#: (game key, emoji, label) for the quick-play row.
QUICK_PLAY = (
    ("slots", "🎰", "Slots"),
    ("roulette", "🎡", "Roulette"),
    ("blackjack", "🃏", "Blackjack"),
    ("mines", "💣", "Mines"),
    ("crash", "🚀", "Crash"),
)

GAME_BLURB = {
    "roulette": "European wheel, single zero. Outside bets pay 1:1 or 2:1, a single number pays 35:1.",
    "blackjack": "Six-deck shoe, dealer stands on 17, blackjack pays 3:2. Double, split and insurance available.",
    "slots": "Three reels, eight symbols. Triple sevens pay x1500.",
    "coinflip": "Heads or tails, resolved instantly.",
    "dice": "Pick a face, or bet that the roll clears a target.",
    "mines": "Reveal tiles for a rising multiplier and bank it before you hit a bomb.",
    "crash": "The multiplier climbs until it crashes. Jump off first.",
    "duel": "Challenge another player to a 50/50 for real stakes. No house cut.",
}


async def hub_embed(
    bot: Any, user: discord.abc.User, balance: int, settings: dict[str, Any]
) -> discord.Embed:
    """The dashboard body, built fresh from live state every time."""
    guild_id = getattr(getattr(user, "guild", None), "id", None)
    level_line = ""
    if guild_id:
        state = await bot.progression.get(guild_id, user.id)
        needed = xp_for_level(state["level"])
        bar = theme.progress_bar(state["xp"], needed)
        prestige = f" {theme.ICON['prestige']}{state['prestige']}" if state["prestige"] else ""
        level_line = (
            f"{theme.ICON['level']} **Level {state['level']}**{prestige}  `{bar}`  "
            f"{state['xp']:,}/{needed:,} XP"
        )

    embed = theme.embed(
        title="🎰 The Casino",
        description=(
            f"{theme.ICON['balance']} **{balance:,}** chips\n"
            + (level_line + "\n" if level_line else "")
            + "\nPick a game below. Every game is provably fair — "
            "each round publishes a hash you can check with `/verify`."
        ),
        color=theme.BRAND,
        author=user,
        thumbnail=getattr(getattr(user, "display_avatar", None), "url", None),
    )
    embed.add_field(
        name="Table limits",
        value=f"{int(settings['min_bet']):,} – {int(settings['max_bet']):,} chips",
        inline=True,
    )
    embed.add_field(
        name="Return to player",
        value=f"{float(settings['rtp']) * 100:.1f}% on house games",
        inline=True,
    )
    embed.add_field(
        name="Daily reward",
        value=f"{int(settings['daily_amount']):,} chips, +10% per streak day",
        inline=True,
    )

    event_name = settings.get("event_name")
    event_ends = parse_iso(settings.get("event_ends_at"))
    if event_name and event_ends:
        embed.add_field(
            name="📣 Server event",
            value=f"**{event_name}** — ends {theme.relative(event_ends)}",
            inline=False,
        )

    embed.set_footer(text="Virtual chips only. No real money is involved. · /help for every command")
    return embed


class GameSelect(discord.ui.Select):
    """Everything not on the quick-play row, plus the rules for each game."""

    def __init__(self) -> None:
        options = [
            discord.SelectOption(label="Coinflip", value="coinflip", emoji="🪙", description=GAME_BLURB["coinflip"][:95]),
            discord.SelectOption(label="Dice", value="dice", emoji="🎲", description=GAME_BLURB["dice"][:95]),
            discord.SelectOption(label="Duel another player", value="duel", emoji="⚔️", description=GAME_BLURB["duel"][:95]),
            discord.SelectOption(label="How every game works", value="rules", emoji="📖", description="Odds, payouts and limits"),
            discord.SelectOption(label="Provable fairness", value="fair", emoji="🔐", description="How to verify a round yourself"),
        ]
        super().__init__(
            placeholder="More games, odds and fairness…",
            options=options,
            custom_id="casino:hub:select",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "HubView" = self.view  # type: ignore[assignment]
        choice = self.values[0]
        if choice == "rules":
            return await view.show_rules(interaction)
        if choice == "fair":
            return await view.show_fairness(interaction)
        if choice == "duel":
            return await interaction.response.send_message(
                embed=theme.embed(
                    title=f"{theme.ICON['duel']} Duels",
                    description=(
                        "Duels need an opponent, so they start from the command:\n"
                        "`/duel @player 1000`\n\n"
                        "Both stakes go into a pot, a coin decides it, and the winner takes "
                        "everything. The house takes nothing, so a duel is the only "
                        "zero-edge game here."
                    ),
                    color=theme.BRAND,
                ),
                ephemeral=True,
            )
        await view.launch(interaction, choice)


class HubView(discord.ui.View):
    """Persistent: registered once at startup, lives forever on one message."""

    def __init__(self, bot: Any) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.add_item(GameSelect())

    # -- plumbing ----------------------------------------------------------

    async def _stake_hint(self, interaction: discord.Interaction) -> str:
        """Pre-fill the stake box with the player's last bet."""
        if interaction.guild_id is None:
            return "100"
        profile = await self.bot.economy.profile(interaction.guild_id, interaction.user.id)
        settings = await self.bot.guilds_store.settings(interaction.guild_id)
        last = int(profile.get("last_stake") or 0)
        return str(last if last >= int(settings["min_bet"]) else int(settings["min_bet"]))

    async def launch(self, interaction: discord.Interaction, game: str) -> None:
        """Send a bet straight into the same handler the slash command uses."""
        if game not in GAME_KEYS:
            return
        if interaction.guild_id is None:
            return await interaction.response.send_message(
                embed=theme.error("The casino only runs inside a server."), ephemeral=True
            )
        # Fail early and clearly if the game is switched off, rather than
        # opening a modal that will be rejected on submit.
        if not await self.bot.guilds_store.game_enabled(interaction.guild_id, game):
            return await interaction.response.send_message(
                embed=theme.error(f"**{game.title()}** is switched off in this server."), ephemeral=True
            )

        games = self.bot.get_cog("Games")
        blackjack = self.bot.get_cog("Blackjack")
        hint = await self._stake_hint(interaction)

        if game == "roulette":
            from casino.ui.games import RouletteBoardView

            board = RouletteBoardView(
                owner_id=interaction.user.id, play=games.play_roulette, stake_hint=hint
            )
            return await interaction.response.send_message(
                embed=theme.embed(
                    title=f"{theme.ICON['roulette']} Roulette",
                    description=(
                        "Choose a bet from the table. Colours and halves pay **1:1**, "
                        "dozens and columns pay **2:1**, a single number pays **35:1**."
                    ),
                    color=theme.TABLE,
                ),
                view=board,
                ephemeral=True,
            )

        handlers = {
            "slots": (games, "play_slots", "Slots"),
            "blackjack": (blackjack, "play_hand", "Blackjack"),
            "mines": (games, "play_mines", "Mines"),
            "crash": (games, "play_crash", "Crash"),
            "coinflip": (games, "play_coinflip", "Coinflip"),
            "dice": (games, "play_dice", "Dice"),
        }
        target = handlers.get(game)
        if target is None or target[0] is None:
            return await interaction.response.send_message(
                embed=theme.error("That game is not loaded right now."), ephemeral=True
            )
        cog, method, label = target
        await interaction.response.send_modal(
            AmountModal(f"{label} · place your bet", getattr(cog, method), default=hint)
        )

    # -- quick play row ----------------------------------------------------

    @discord.ui.button(label="Slots", emoji="🎰", style=discord.ButtonStyle.primary, row=1, custom_id="casino:hub:slots")
    async def slots(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.launch(interaction, "slots")

    @discord.ui.button(label="Roulette", emoji="🎡", style=discord.ButtonStyle.primary, row=1, custom_id="casino:hub:roulette")
    async def roulette(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.launch(interaction, "roulette")

    @discord.ui.button(label="Blackjack", emoji="🃏", style=discord.ButtonStyle.primary, row=1, custom_id="casino:hub:blackjack")
    async def blackjack(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.launch(interaction, "blackjack")

    @discord.ui.button(label="Mines", emoji="💣", style=discord.ButtonStyle.primary, row=1, custom_id="casino:hub:mines")
    async def mines(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.launch(interaction, "mines")

    @discord.ui.button(label="Crash", emoji="🚀", style=discord.ButtonStyle.primary, row=1, custom_id="casino:hub:crash")
    async def crash(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.launch(interaction, "crash")

    # -- account row -------------------------------------------------------

    @discord.ui.button(label="Wallet", emoji="💰", style=discord.ButtonStyle.secondary, row=2, custom_id="casino:hub:wallet")
    async def wallet(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        economy = self.bot.get_cog("Economy")
        await economy.show_wallet(interaction)

    @discord.ui.button(label="Daily", emoji="🎁", style=discord.ButtonStyle.success, row=2, custom_id="casino:hub:daily")
    async def daily(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        economy = self.bot.get_cog("Economy")
        await economy.do_daily(interaction)

    @discord.ui.button(label="Work", emoji="💼", style=discord.ButtonStyle.success, row=2, custom_id="casino:hub:work")
    async def work(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        economy = self.bot.get_cog("Economy")
        await economy.do_work(interaction)

    @discord.ui.button(label="Profile", emoji="📇", style=discord.ButtonStyle.secondary, row=2, custom_id="casino:hub:profile")
    async def profile(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        progression = self.bot.get_cog("Progression")
        await progression.show_profile(interaction, interaction.user)

    # -- info row ----------------------------------------------------------

    @discord.ui.button(label="Leaderboard", emoji="🏆", style=discord.ButtonStyle.secondary, row=3, custom_id="casino:hub:leaderboard")
    async def leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        boards = self.bot.get_cog("Leaderboards")
        await boards.show_board(interaction, "balance", "alltime")

    @discord.ui.button(label="Challenges", emoji="🎯", style=discord.ButtonStyle.secondary, row=3, custom_id="casino:hub:challenges")
    async def challenges(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        progression = self.bot.get_cog("Progression")
        await progression.show_challenges(interaction)

    @discord.ui.button(label="Shop", emoji="🛍️", style=discord.ButtonStyle.secondary, row=3, custom_id="casino:hub:shop")
    async def shop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        progression = self.bot.get_cog("Progression")
        await progression.show_shop(interaction)

    @discord.ui.button(label="Timers", emoji="⏱️", style=discord.ButtonStyle.secondary, row=3, custom_id="casino:hub:timers")
    async def timers(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        economy = self.bot.get_cog("Economy")
        await economy.show_timers(interaction)

    # -- reference screens -------------------------------------------------

    async def show_rules(self, interaction: discord.Interaction) -> None:
        settings = await self.bot.guilds_store.settings(interaction.guild_id)
        enabled = await self.bot.guilds_store.games(interaction.guild_id) if interaction.guild_id else {}
        lines = []
        for key in GAME_KEYS:
            state = "" if enabled.get(key, True) else " *(off)*"
            lines.append(f"**{key.title()}**{state}\n{GAME_BLURB[key]}")
        embed = theme.embed(
            title="📖 How every game works",
            description="\n\n".join(lines),
            color=theme.BRAND,
            footer=(
                f"Limits {int(settings['min_bet']):,}–{int(settings['max_bet']):,} · "
                f"house games return {float(settings['rtp']) * 100:.1f}%"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def show_fairness(self, interaction: discord.Interaction) -> None:
        embed = theme.embed(
            title=f"{theme.ICON['fair']} Provable fairness",
            description=(
                "Every round is generated from a secret **server seed**, and the "
                "footer of each result shows the first 16 characters of that seed's "
                "SHA-256 hash. Because the hash is published before the outcome is "
                "known, the bot cannot change the seed after seeing your bet.\n\n"
                "**To check a round**\n"
                "1. Run `/verify` and paste the revealed seed and the commitment.\n"
                "2. The bot re-hashes the seed in front of you.\n"
                "3. Any mismatch means the result was tampered with.\n\n"
                "The seeds come from the operating system's cryptographic random "
                "source, not from a clock-seeded generator."
            ),
            color=theme.INFO,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
