"""Storage for the moderation and support features: warnings and tickets."""

from __future__ import annotations

from typing import Any

from casino.db.database import Database
from casino.db.economy import iso, utcnow


class Moderation:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def warn(self, guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
        """Record a warning and return the player's new total for this guild.

        Warnings used to be stored globally, keyed only by user id, so a
        warning in one server showed up in every other server the bot was in.
        They are now scoped per guild like everything else.
        """
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, moderator_id, reason[:1000], iso(utcnow())),
            )
            row = await (await conn.execute(
                "SELECT COUNT(*) AS total FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )).fetchone()
        return int(row["total"])

    async def warnings(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
            (guild_id, user_id),
        )
        return [dict(row) for row in rows]

    async def clear_warnings(self, guild_id: int, user_id: int) -> int:
        async with self.db.transaction() as conn:
            row = await (await conn.execute(
                "SELECT COUNT(*) AS total FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )).fetchone()
            await conn.execute("DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return int(row["total"])

    async def delete_warning(self, guild_id: int, warning_id: int) -> bool:
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM warnings WHERE guild_id = ? AND id = ?", (guild_id, warning_id)
            )
        return cursor.rowcount > 0

    async def recent(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM warnings WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit)
        )
        return [dict(row) for row in rows]


class Tickets:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, guild_id: int, channel_id: int, user_id: int, category: str) -> None:
        await self.db.execute(
            "INSERT INTO tickets (channel_id, guild_id, user_id, category, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (channel_id) DO UPDATE SET user_id = excluded.user_id, category = excluded.category",
            (channel_id, guild_id, user_id, category, iso(utcnow())),
        )

    async def get(self, channel_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,))
        return dict(row) if row else None

    async def open_for(self, guild_id: int, user_id: int) -> int | None:
        return await self.db.fetchval(
            "SELECT channel_id FROM tickets WHERE guild_id = ? AND user_id = ? AND closed_at IS NULL",
            (guild_id, user_id),
        )

    async def claim(self, channel_id: int, moderator_id: int) -> None:
        await self.db.execute(
            "UPDATE tickets SET claimed_by = ? WHERE channel_id = ?", (moderator_id, channel_id)
        )

    async def set_closed(self, channel_id: int, closed: bool) -> None:
        await self.db.execute(
            "UPDATE tickets SET closed_at = ? WHERE channel_id = ?",
            (iso(utcnow()) if closed else None, channel_id),
        )

    async def delete(self, channel_id: int) -> None:
        await self.db.execute("DELETE FROM tickets WHERE channel_id = ?", (channel_id,))

    async def count_open(self, guild_id: int) -> int:
        return int(await self.db.fetchval(
            "SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND closed_at IS NULL", (guild_id,), 0
        ))
