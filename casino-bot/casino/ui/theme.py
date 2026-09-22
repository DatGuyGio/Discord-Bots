"""Visual language for every embed and component the bot sends.

The old bot picked colours and emoji ad hoc at each call site, so two screens
describing the same thing rarely looked alike. Everything visual now comes
from this module: one palette, one set of icons, one embed factory.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------

BRAND = discord.Color(0x5B4BFF)  # primary / neutral screens
WIN = discord.Color(0x2ECC71)
LOSE = discord.Color(0xE8433F)
PUSH = discord.Color(0x8E9297)
WARN = discord.Color(0xF5A623)
INFO = discord.Color(0x3BA9F4)
GOLD = discord.Color(0xF1C40F)
TABLE = discord.Color(0x1F7A4D)  # felt green, used for card games
DANGER = discord.Color(0x992D22)

ICON = {
    "chips": "🪙",
    "balance": "💰",
    "win": "🎉",
    "lose": "💥",
    "push": "🔄",
    "roulette": "🎡",
    "blackjack": "🃏",
    "slots": "🎰",
    "coinflip": "🪙",
    "dice": "🎲",
    "mines": "💣",
    "crash": "🚀",
    "duel": "⚔️",
    "daily": "🎁",
    "work": "💼",
    "level": "⭐",
    "prestige": "👑",
    "trophy": "🏆",
    "streak": "🔥",
    "shop": "🛍️",
    "ticket": "🎫",
    "shield": "🛡️",
    "clock": "⏱️",
    "warn": "⚠️",
    "ok": "✅",
    "no": "⛔",
    "fair": "🔐",
    "up": "📈",
    "down": "📉",
    "gem": "💎",
}

MEDALS = ("🥇", "🥈", "🥉")

# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def chips(amount: int | float) -> str:
    """``12,345 🪙`` — the canonical way to render an amount of chips."""
    return f"{int(amount):,} {ICON['chips']}"


def signed(amount: int | float) -> str:
    """``+1,200`` / ``-350`` with an explicit sign, for profit and loss."""
    return f"{int(amount):+,}"


def compact(amount: int | float) -> str:
    """Short form for tight spaces: ``1.2M``, ``345K``, ``980``."""
    amount = int(amount)
    sign = "-" if amount < 0 else ""
    value = abs(amount)
    for limit, suffix in ((1_000_000_000_000, "T"), (1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= limit:
            trimmed = f"{value / limit:.1f}".rstrip("0").rstrip(".")
            return f"{sign}{trimmed}{suffix}"
    return f"{sign}{value}"


def multiplier(value: float) -> str:
    """``x1.98`` — trailing zeros trimmed so x2.00 reads as x2."""
    return f"x{value:.2f}".rstrip("0").rstrip(".").replace("x.", "x0.")


def percent(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


def progress_bar(current: float, total: float, width: int = 12) -> str:
    """A filled/empty block bar. Used for XP, challenges, and level progress."""
    if total <= 0:
        ratio = 1.0
    else:
        ratio = max(0.0, min(1.0, current / total))
    filled = round(ratio * width)
    return "▰" * filled + "▱" * (width - filled)


def relative(moment: datetime) -> str:
    """Discord's own relative timestamp, so every client localises it."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return discord.utils.format_dt(moment, "R")


def bullet_list(lines: list[str]) -> str:
    return "\n".join(f"• {line}" for line in lines)


# --------------------------------------------------------------------------
# Embed factory
# --------------------------------------------------------------------------


def embed(
    *,
    title: str | None = None,
    description: str | None = None,
    color: discord.Color = BRAND,
    footer: str | None = None,
    thumbnail: str | None = None,
    author: discord.abc.User | None = None,
    author_text: str | None = None,
    timestamp: bool = False,
) -> discord.Embed:
    """Build an embed that looks like every other embed this bot sends."""
    result = discord.Embed(title=title, description=description, color=color)
    if timestamp:
        result.timestamp = datetime.now(timezone.utc)
    if author is not None:
        result.set_author(
            name=author_text or getattr(author, "display_name", str(author)),
            icon_url=getattr(getattr(author, "display_avatar", None), "url", None),
        )
    elif author_text:
        result.set_author(name=author_text)
    if thumbnail:
        result.set_thumbnail(url=thumbnail)
    if footer:
        result.set_footer(text=footer)
    return result


def error(message: str, *, title: str | None = None) -> discord.Embed:
    return embed(title=title or f"{ICON['warn']} Hold on", description=message, color=WARN)


def denied(message: str) -> discord.Embed:
    return embed(title=f"{ICON['no']} Not allowed", description=message, color=DANGER)


def success(message: str, *, title: str | None = None) -> discord.Embed:
    return embed(title=title or f"{ICON['ok']} Done", description=message, color=WIN)


def outcome_color(net: int) -> discord.Color:
    if net > 0:
        return WIN
    if net < 0:
        return LOSE
    return PUSH


def outcome_icon(net: int) -> str:
    if net > 0:
        return ICON["win"]
    if net < 0:
        return ICON["lose"]
    return ICON["push"]


def result_block(stake: int, net: int, balance: int) -> str:
    """The three lines that close out every game embed, in the same order."""
    verdict = "Profit" if net > 0 else "Loss" if net < 0 else "Returned"
    return (
        f"**Stake** {chips(stake)}\n"
        f"**{verdict}** {signed(net)}\n"
        f"**Balance** {chips(balance)}"
    )
