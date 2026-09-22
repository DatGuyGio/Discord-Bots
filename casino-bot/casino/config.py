"""Runtime configuration.

Everything the bot needs to boot lives here. Secrets come from the
environment (or a local ``.env``); gameplay tuning lives in the database so
each server can change it without a restart. The values below are only the
defaults a brand-new server starts with.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # optional convenience for local development
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is not required in production
    pass


def _int_set(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(part) for part in raw.replace(",", " ").split() if part.strip().isdigit()}


@dataclass(slots=True)
class Settings:
    """Process-level settings, read once at startup."""

    token: str = field(default_factory=lambda: os.environ.get("DISCORD_BOT_TOKEN", ""))
    prefix: str = field(default_factory=lambda: os.environ.get("CASINO_PREFIX", "!"))
    database_path: str = field(default_factory=lambda: os.environ.get("CASINO_DB", "casino.sqlite3"))
    owner_ids: set[int] = field(default_factory=lambda: _int_set(os.environ.get("CASINO_OWNER_IDS")))
    # Syncing slash commands to a single guild is instant; a global sync can
    # take up to an hour to appear. Set this while developing.
    dev_guild_id: int | None = field(
        default_factory=lambda: int(os.environ["CASINO_DEV_GUILD"])
        if os.environ.get("CASINO_DEV_GUILD", "").isdigit()
        else None
    )
    log_level: str = field(default_factory=lambda: os.environ.get("CASINO_LOG_LEVEL", "INFO"))

    def require_token(self) -> str:
        if not self.token:
            raise SystemExit(
                "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and add your bot token."
            )
        return self.token


settings = Settings()


# --------------------------------------------------------------------------
# Economy defaults (per guild; editable at runtime with /config)
# --------------------------------------------------------------------------

#: Fraction of every wager returned to players over the long run. Games derive
#: their payouts from this number instead of hardcoding multipliers, so the
#: house edge is one obvious knob rather than a dozen scattered constants.
DEFAULT_RTP = 0.97

GUILD_DEFAULTS: dict[str, object] = {
    "starting_balance": 2_500,
    "min_bet": 10,
    "max_bet": 250_000,
    "daily_amount": 1_000,
    "daily_streak_bonus": 0.10,  # +10% per consecutive day, capped at 2x
    "work_min": 250,
    "work_max": 750,
    "work_cooldown": 900,  # seconds
    "upgrade_base_cost": 2_500,
    "upgrade_cost_growth": 1.55,
    "upgrade_multiplier_step": 0.25,
    "rtp": DEFAULT_RTP,
    "maintenance": 0,
    "casino_channel": None,
    "logs_channel": None,
    "welcome_channel": None,
    "welcome_message": "Welcome {user} to **{server}**! You are member #{membercount}.",
    "goodbye_channel": None,
    "goodbye_message": "**{username}** just left {server}.",
    "ticket_category": None,
    "ticket_support_role": None,
    "ticket_transcript_channel": None,
    "ticket_message": "Thanks for opening a ticket. A staff member will be with you shortly.",
    "admin_role": None,
    "event_name": None,
    "event_ends_at": None,
}

#: Games can be switched off individually per guild.
GAME_KEYS = ("roulette", "blackjack", "slots", "coinflip", "dice", "mines", "crash", "duel")

#: Log categories that can be toggled with /logconfig.
LOG_KEYS = ("messages", "members", "moderation", "roles", "channels", "server", "casino")

#: How many recent ledger rows a player can page through.
LEDGER_PAGE_SIZE = 8

#: Hard ceiling on any single stored amount. The old JSON files contained a
#: 10^27 wager from an exploited bet; clamping keeps arithmetic and formatting
#: sane and makes an exploit obvious instead of permanent.
MAX_CHIPS = 1_000_000_000_000  # one trillion
