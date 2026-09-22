"""Levels, titles, cosmetics, achievements, and the responsible-play limits.

Two things worth calling out.

*Levels* use a rising curve (``level * 120`` XP for the next level) rather
than a flat one, so the number keeps meaning something at level 40.

*Limits* are the reason this module is separate from the economy. A casino
ban is an admin action; self-exclusion and a daily wager cap are the
player's own choices and are deliberately one-way — nothing in the bot can
shorten them, including a full account reset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from casino.db.database import Database
from casino.db.economy import iso, parse_iso, utcnow

XP_PER_LEVEL = 120


def xp_for_level(level: int) -> int:
    """XP needed to move from ``level`` to ``level + 1``."""
    return level * XP_PER_LEVEL


@dataclass(frozen=True, slots=True)
class ShopItem:
    id: str
    name: str
    description: str
    price: int
    kind: str  # title | badge | perk
    level_required: int = 0


SHOP: tuple[ShopItem, ...] = (
    ShopItem("title_rookie", "Rookie", "A humble start.", 1_000, "title"),
    ShopItem("title_highroller", "High Roller", "For those who bet big.", 25_000, "title", 5),
    ShopItem("title_whale", "Whale", "The tables notice when you sit down.", 150_000, "title", 15),
    ShopItem("title_legend", "Legend", "Reserved for the truly reckless.", 750_000, "title", 30),
    ShopItem("badge_gold", "Gold Trim", "A gold accent on your profile card.", 15_000, "badge"),
    ShopItem("badge_neon", "Neon Trim", "A neon accent on your profile card.", 40_000, "badge", 10),
    ShopItem("badge_royal", "Royal Trim", "A royal accent on your profile card.", 120_000, "badge", 20),
)

SHOP_BY_ID = {item.id: item for item in SHOP}

BADGE_EMOJI = {"badge_gold": "🟡", "badge_neon": "🟣", "badge_royal": "👑"}


@dataclass(frozen=True, slots=True)
class Achievement:
    id: str
    name: str
    description: str
    icon: str
    #: Given a profile dict, has this been earned?
    test: Any

    def earned(self, profile: dict[str, Any]) -> bool:
        return bool(self.test(profile))


ACHIEVEMENTS: tuple[Achievement, ...] = (
    Achievement("first_game", "First Spin", "Play your first game.", "🎰", lambda p: p.get("games", 0) >= 1),
    Achievement("ten_games", "Getting Started", "Play 10 games.", "🎯", lambda p: p.get("games", 0) >= 10),
    Achievement("hundred_games", "Regular", "Play 100 games.", "🎲", lambda p: p.get("games", 0) >= 100),
    Achievement("thousand_games", "Fixture", "Play 1,000 games.", "🏛️", lambda p: p.get("games", 0) >= 1_000),
    Achievement("million_wagered", "High Roller", "Wager 1,000,000 chips.", "💰", lambda p: p.get("wagered", 0) >= 1_000_000),
    Achievement("ten_million_wagered", "Whale", "Wager 10,000,000 chips.", "💎", lambda p: p.get("wagered", 0) >= 10_000_000),
    Achievement("millionaire", "Millionaire", "Hold 1,000,000 chips at once.", "🤑", lambda p: p.get("balance", 0) >= 1_000_000),
    Achievement("streak_10", "On Fire", "Win 10 games in a row.", "🔥", lambda p: p.get("best_streak", 0) >= 10),
    Achievement("streak_25", "Untouchable", "Win 25 games in a row.", "🧊", lambda p: p.get("best_streak", 0) >= 25),
    Achievement("comeback", "Comeback", "Finish a session in profit overall.", "📈", lambda p: p.get("net", 0) > 0 and p.get("games", 0) >= 25),
    Achievement("week_streak", "Loyal", "Claim the daily reward 7 days running.", "📅", lambda p: p.get("daily_streak", 0) >= 7),
)

ACHIEVEMENTS_BY_ID = {item.id: item for item in ACHIEVEMENTS}


class Progression:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- core row ----------------------------------------------------------

    async def get(self, guild_id: int, user_id: int) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT * FROM progression WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        if row is None:
            await self.db.execute(
                "INSERT INTO progression (guild_id, user_id) VALUES (?, ?) "
                "ON CONFLICT (guild_id, user_id) DO NOTHING",
                (guild_id, user_id),
            )
            return {
                "guild_id": guild_id, "user_id": user_id, "xp": 0, "level": 1, "prestige": 0,
                "title": None, "badge": None, "casino_ban_until": None,
                "self_exclude_until": None, "daily_wager_limit": None, "suspicion": 0,
            }
        return dict(row)

    async def award_xp(self, guild_id: int, user_id: int, amount: int) -> tuple[int, int, bool]:
        """Add XP. Returns ``(level, xp_into_level, levelled_up)``."""
        amount = max(0, int(amount))
        async with self.db.transaction() as conn:
            row = await (await conn.execute(
                "SELECT xp, level FROM progression WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )).fetchone()
            xp = int(row["xp"]) if row else 0
            level = int(row["level"]) if row else 1
            before = level
            xp += amount
            while xp >= xp_for_level(level):
                xp -= xp_for_level(level)
                level += 1
            await conn.execute(
                "INSERT INTO progression (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id) DO UPDATE SET xp = excluded.xp, level = excluded.level",
                (guild_id, user_id, xp, level),
            )
        return level, xp, level > before

    async def xp_for_round(self, settings: dict[str, Any], stake: int, won: bool) -> int:
        base = 10 + (15 if won else 0)
        return base + int(max(0, stake) // 1_000)

    async def prestige(self, guild_id: int, user_id: int) -> int:
        async with self.db.transaction() as conn:
            row = await (await conn.execute(
                "SELECT prestige FROM progression WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )).fetchone()
            value = (int(row["prestige"]) if row else 0) + 1
            await conn.execute(
                "UPDATE progression SET prestige = ?, level = 1, xp = 0 WHERE guild_id = ? AND user_id = ?",
                (value, guild_id, user_id),
            )
        return value

    # -- cosmetics ---------------------------------------------------------

    async def inventory(self, guild_id: int, user_id: int) -> set[str]:
        rows = await self.db.fetchall(
            "SELECT item_id FROM inventory WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        return {row["item_id"] for row in rows}

    async def grant(self, guild_id: int, user_id: int, item_id: str) -> None:
        await self.db.execute(
            "INSERT INTO inventory (guild_id, user_id, item_id, acquired_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id, item_id) DO NOTHING",
            (guild_id, user_id, item_id, iso(utcnow())),
        )

    async def equip(self, guild_id: int, user_id: int, item: ShopItem) -> None:
        column = "title" if item.kind == "title" else "badge"
        await self.db.execute(
            f"UPDATE progression SET {column} = ? WHERE guild_id = ? AND user_id = ?",
            (item.id, guild_id, user_id),
        )

    # -- achievements ------------------------------------------------------

    async def unlocked(self, guild_id: int, user_id: int) -> set[str]:
        rows = await self.db.fetchall(
            "SELECT achievement_id FROM achievements WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        return {row["achievement_id"] for row in rows}

    async def evaluate(self, guild_id: int, user_id: int, profile: dict[str, Any]) -> list[Achievement]:
        """Unlock anything newly earned and return just the new ones."""
        already = await self.unlocked(guild_id, user_id)
        fresh = [a for a in ACHIEVEMENTS if a.id not in already and a.earned(profile)]
        if fresh:
            now = iso(utcnow())
            await self.db.executemany(
                "INSERT INTO achievements (guild_id, user_id, achievement_id, unlocked_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id, achievement_id) DO NOTHING",
                [(guild_id, user_id, a.id, now) for a in fresh],
            )
        return fresh

    # -- limits and bans ---------------------------------------------------

    async def set_casino_ban(self, guild_id: int, user_id: int, until: datetime | None) -> None:
        await self.db.execute(
            "INSERT INTO progression (guild_id, user_id, casino_ban_until) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET casino_ban_until = excluded.casino_ban_until",
            (guild_id, user_id, iso(until) if until else None),
        )

    async def set_self_exclusion(self, guild_id: int, user_id: int, duration: timedelta) -> datetime:
        """Start (or extend) a self-exclusion. Never shortens an active one."""
        current = await self.get(guild_id, user_id)
        existing = parse_iso(current.get("self_exclude_until"))
        until = utcnow() + duration
        if existing and existing > until:
            return existing
        await self.db.execute(
            "INSERT INTO progression (guild_id, user_id, self_exclude_until) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET self_exclude_until = excluded.self_exclude_until",
            (guild_id, user_id, iso(until)),
        )
        return until

    async def set_wager_limit(self, guild_id: int, user_id: int, limit: int | None) -> None:
        await self.db.execute(
            "INSERT INTO progression (guild_id, user_id, daily_wager_limit) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO UPDATE SET daily_wager_limit = excluded.daily_wager_limit",
            (guild_id, user_id, int(limit) if limit else None),
        )

    async def add_suspicion(self, guild_id: int, user_id: int, amount: int) -> int:
        async with self.db.transaction() as conn:
            row = await (await conn.execute(
                "SELECT suspicion FROM progression WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )).fetchone()
            value = min(100, (int(row["suspicion"]) if row else 0) + int(amount))
            await conn.execute(
                "INSERT INTO progression (guild_id, user_id, suspicion) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id, user_id) DO UPDATE SET suspicion = excluded.suspicion",
                (guild_id, user_id, value),
            )
        return value

    async def leaderboard(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT user_id, level, xp, prestige FROM progression WHERE guild_id = ? "
            "ORDER BY prestige DESC, level DESC, xp DESC LIMIT ?",
            (guild_id, limit),
        )
        return [dict(row) for row in rows]
