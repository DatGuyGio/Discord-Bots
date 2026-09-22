"""Per-guild configuration.

Settings live as a JSON document per guild, validated against
:data:`casino.config.GUILD_DEFAULTS` on write and cached in memory on read.
That gives three things the old bot lacked: unknown keys cannot be written,
missing keys always fall back to a default (so a new setting needs no data
migration), and the hot path — "what is the minimum bet here?" — does not hit
the disk on every single command.
"""

from __future__ import annotations

import json
from typing import Any

from casino.config import GAME_KEYS, GUILD_DEFAULTS, LOG_KEYS
from casino.db.database import Database
from casino.db.economy import iso, utcnow


class UnknownSetting(KeyError):
    pass


class GuildStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: dict[int, dict[str, Any]] = {}
        self._games: dict[int, dict[str, bool]] = {}
        self._logs: dict[int, dict[str, bool]] = {}

    # -- settings ----------------------------------------------------------

    async def settings(self, guild_id: int | None) -> dict[str, Any]:
        """Merged settings for a guild. Safe to call on every command."""
        if guild_id is None:
            return dict(GUILD_DEFAULTS)
        if guild_id in self._cache:
            return self._cache[guild_id]
        raw = await self.db.fetchval("SELECT data FROM guild_settings WHERE guild_id = ?", (guild_id,))
        stored: dict[str, Any] = json.loads(raw) if raw else {}
        merged = {**GUILD_DEFAULTS, **{k: v for k, v in stored.items() if k in GUILD_DEFAULTS}}
        self._cache[guild_id] = merged
        return merged

    async def update(self, guild_id: int, **changes: Any) -> dict[str, Any]:
        unknown = set(changes) - set(GUILD_DEFAULTS)
        if unknown:
            raise UnknownSetting(f"unknown setting(s): {', '.join(sorted(unknown))}")
        current = dict(await self.settings(guild_id))
        current.update(changes)
        payload = json.dumps({k: v for k, v in current.items() if v != GUILD_DEFAULTS[k]})
        await self.db.execute(
            "INSERT INTO guild_settings (guild_id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (guild_id, payload, iso(utcnow())),
        )
        self._cache[guild_id] = current
        return current

    async def reset(self, guild_id: int) -> dict[str, Any]:
        await self.db.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
        self._cache.pop(guild_id, None)
        return await self.settings(guild_id)

    # -- per-game switches -------------------------------------------------

    async def games(self, guild_id: int) -> dict[str, bool]:
        if guild_id in self._games:
            return self._games[guild_id]
        rows = await self.db.fetchall("SELECT game, enabled FROM guild_games WHERE guild_id = ?", (guild_id,))
        state = {key: True for key in GAME_KEYS}
        for row in rows:
            if row["game"] in state:
                state[row["game"]] = bool(row["enabled"])
        self._games[guild_id] = state
        return state

    async def game_enabled(self, guild_id: int | None, game: str) -> bool:
        if guild_id is None:
            return True
        return (await self.games(guild_id)).get(game, True)

    async def set_game(self, guild_id: int, game: str, enabled: bool) -> None:
        if game not in GAME_KEYS:
            raise UnknownSetting(f"unknown game: {game}")
        await self.db.execute(
            "INSERT INTO guild_games (guild_id, game, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, game) DO UPDATE SET enabled = excluded.enabled",
            (guild_id, game, int(enabled)),
        )
        self._games.setdefault(guild_id, {key: True for key in GAME_KEYS})[game] = enabled

    # -- logging switches --------------------------------------------------

    async def logs(self, guild_id: int) -> dict[str, bool]:
        if guild_id in self._logs:
            return self._logs[guild_id]
        rows = await self.db.fetchall("SELECT kind, enabled FROM guild_logs WHERE guild_id = ?", (guild_id,))
        state = {key: True for key in LOG_KEYS}
        for row in rows:
            if row["kind"] in state:
                state[row["kind"]] = bool(row["enabled"])
        self._logs[guild_id] = state
        return state

    async def log_enabled(self, guild_id: int, kind: str) -> bool:
        return (await self.logs(guild_id)).get(kind, True)

    async def set_log(self, guild_id: int, kind: str, enabled: bool) -> None:
        if kind not in LOG_KEYS:
            raise UnknownSetting(f"unknown log category: {kind}")
        await self.db.execute(
            "INSERT INTO guild_logs (guild_id, kind, enabled) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, kind) DO UPDATE SET enabled = excluded.enabled",
            (guild_id, kind, int(enabled)),
        )
        self._logs.setdefault(guild_id, {key: True for key in LOG_KEYS})[kind] = enabled

    def invalidate(self, guild_id: int) -> None:
        self._cache.pop(guild_id, None)
        self._games.pop(guild_id, None)
        self._logs.pop(guild_id, None)
