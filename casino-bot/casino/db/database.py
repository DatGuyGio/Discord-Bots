"""The SQLite connection, and the transaction primitive everything builds on.

Why move off JSON at all? The old ``Bank`` kept the whole economy in a dict,
took an ``asyncio.Lock``, mutated the dict, and rewrote the entire file with
``json.dump`` on *every* balance change — several times per spin. That has
three problems that matter in practice:

1. A crash or a full disk mid-write truncates the file and loses everything.
2. Cost grows with the size of the whole file, not the size of the change.
3. Any answer that is not "one player's current row" needs a Python loop over
   every player, which is why the old windowed leaderboard walked every
   transaction of every user on every invocation.

SQLite fixes all three and gives real transactions, which is what lets a
wager debit and its settlement be atomic.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Sequence

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    """Thin async wrapper around one SQLite connection."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        # SQLite allows a single writer. Serialising write transactions here
        # means a concurrent BEGIN IMMEDIATE never has to spin on a busy lock.
        self._write_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected; call connect() first")
        return self._conn

    async def connect(self) -> "Database":
        # isolation_level=None puts the driver in autocommit mode so that
        # transaction boundaries are explicit and visible in this file.
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self.apply_schema()
        log.info("database ready at %s", self.path)
        return self

    async def apply_schema(self) -> None:
        await self.conn.executescript(SCHEMA_PATH.read_text())

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -- queries -----------------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        await self.conn.executemany(sql, params)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def fetchval(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    # -- transactions ------------------------------------------------------

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run a block as one atomic write.

        ``BEGIN IMMEDIATE`` takes the write lock up front, so two players
        clicking at the same instant are serialised rather than interleaved —
        the property that makes :meth:`casino.db.economy.Economy.place_wager`
        safe against double-spending.
        """
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    # -- housekeeping ------------------------------------------------------

    async def get_meta(self, key: str, default: str | None = None) -> str | None:
        return await self.fetchval("SELECT value FROM meta WHERE key = ?", (key,), default)

    async def set_meta(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def vacuum(self) -> None:
        await self.conn.execute("VACUUM")
