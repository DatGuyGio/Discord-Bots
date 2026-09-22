"""The chip ledger: balances, wagers, payouts, and everything derived from them.

The single most important function here is :meth:`Economy.place_wager`. In
the old bot a spin looked like this::

    await bank.add_balance(user, -stake)     # debit
    ...                                      # play the game
    await bank.add_balance(user, stake + win) # credit

``add_balance`` was read-modify-write with no constraint, so two clicks that
arrived while the first spin was still animating could both pass the balance
check and both debit — and because nothing stopped the balance going below
zero, the result was a negative balance or free chips. The stored data still
shows the scar: one player's ``total_wagered`` is 10^27.

Here a wager is a conditional UPDATE inside ``BEGIN IMMEDIATE``::

    UPDATE players SET balance = balance - :stake
     WHERE guild_id = :g AND user_id = :u AND balance >= :stake

If that statement changes zero rows the player could not afford the bet, and
the transaction is rolled back. Two simultaneous clicks are serialised by the
write lock, so the second one sees the first one's debit.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Sequence

from casino.config import MAX_CHIPS
from casino.core.errors import InsufficientFunds, OnCooldown
from casino.db.database import Database

Metric = Literal["balance", "wagered", "net", "wins", "streak"]

METRIC_COLUMNS: dict[str, tuple[str, str]] = {
    # metric -> (table, column)
    "balance": ("players", "balance"),
    "wagered": ("stats", "wagered"),
    "net": ("stats", "net"),
    "wins": ("stats", "wins"),
    "streak": ("stats", "best_streak"),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def period_keys(moment: datetime | None = None) -> dict[str, str]:
    moment = moment or utcnow()
    calendar = moment.isocalendar()
    return {
        "daily": moment.date().isoformat(),
        "weekly": f"{calendar.year}-W{calendar.week:02d}",
    }


@dataclass(slots=True)
class Wager:
    """A stake that has been taken from a balance but not yet resolved.

    Holding this object means the chips are already out of the player's
    balance, so the game can take as long as it likes (a blackjack hand, a
    mines round, a crash ride) with no risk of the player spending them twice.
    """

    guild_id: int
    user_id: int
    stake: int
    game: str
    balance_after_debit: int
    detail: str | None = None
    settled: bool = False


@dataclass(slots=True)
class Settlement:
    """What the player sees after a round is priced."""

    stake: int
    payout: int
    net: int
    balance: int
    outcome: str
    game: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def won(self) -> bool:
        return self.net > 0

    @property
    def pushed(self) -> bool:
        return self.net == 0


class Economy:
    """All reads and writes against chips, stats, and the ledger."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- player rows -------------------------------------------------------

    async def ensure(self, guild_id: int, user_id: int, starting_balance: int) -> None:
        """Create the player's rows if this is their first interaction."""
        now = iso(utcnow())
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO players (guild_id, user_id, balance, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id) DO NOTHING",
                (guild_id, user_id, int(starting_balance), now),
            )
            await conn.execute(
                "INSERT INTO stats (guild_id, user_id) VALUES (?, ?) "
                "ON CONFLICT (guild_id, user_id) DO NOTHING",
                (guild_id, user_id),
            )
            await conn.execute(
                "INSERT INTO progression (guild_id, user_id) VALUES (?, ?) "
                "ON CONFLICT (guild_id, user_id) DO NOTHING",
                (guild_id, user_id),
            )

    async def balance(self, guild_id: int, user_id: int) -> int:
        return int(
            await self.db.fetchval(
                "SELECT balance FROM players WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
                0,
            )
        )

    async def profile(self, guild_id: int, user_id: int) -> dict[str, Any]:
        """Player row joined with stats, plus a few derived fields."""
        row = await self.db.fetchone(
            """
            SELECT p.*, 
                   COALESCE(s.games, 0) AS games,
                   COALESCE(s.wins, 0) AS wins,
                   COALESCE(s.losses, 0) AS losses,
                   COALESCE(s.pushes, 0) AS pushes,
                   COALESCE(s.wagered, 0) AS wagered,
                   COALESCE(s.net, 0) AS net,
                   COALESCE(s.biggest_win, 0) AS biggest_win,
                   COALESCE(s.biggest_loss, 0) AS biggest_loss,
                   COALESCE(s.streak, 0) AS streak,
                   COALESCE(s.best_streak, 0) AS best_streak,
                   s.last_game AS last_game,
                   COALESCE(s.last_stake, 0) AS last_stake,
                   s.last_bet_detail AS last_bet_detail
              FROM players p
              LEFT JOIN stats s ON s.guild_id = p.guild_id AND s.user_id = p.user_id
             WHERE p.guild_id = ? AND p.user_id = ?
            """,
            (guild_id, user_id),
        )
        data: dict[str, Any] = dict(row) if row else {}
        data["favourite_game"] = await self.favourite_game(guild_id, user_id)
        return data

    async def favourite_game(self, guild_id: int, user_id: int) -> str | None:
        return await self.db.fetchval(
            "SELECT game FROM game_plays WHERE guild_id = ? AND user_id = ? "
            "ORDER BY plays DESC, wagered DESC LIMIT 1",
            (guild_id, user_id),
        )

    async def game_breakdown(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT game, plays, wagered, net FROM game_plays "
            "WHERE guild_id = ? AND user_id = ? ORDER BY plays DESC",
            (guild_id, user_id),
        )
        return [dict(row) for row in rows]

    # -- balance movements -------------------------------------------------

    async def _record(
        self,
        conn: Any,
        guild_id: int,
        user_id: int,
        *,
        kind: str,
        delta: int,
        balance_after: int,
        game: str | None = None,
        stake: int = 0,
        note: str | None = None,
    ) -> None:
        await conn.execute(
            "INSERT INTO ledger (guild_id, user_id, kind, game, stake, delta, balance_after, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, kind, game, int(stake), int(delta), int(balance_after), note, iso(utcnow())),
        )

    async def adjust(
        self,
        guild_id: int,
        user_id: int,
        delta: int,
        *,
        kind: str,
        game: str | None = None,
        note: str | None = None,
        allow_negative: bool = False,
    ) -> int:
        """Move chips in or out and write one ledger row. Returns the new balance."""
        delta = int(delta)
        async with self.db.transaction() as conn:
            row = await (await conn.execute(
                "SELECT balance FROM players WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )).fetchone()
            current = int(row["balance"]) if row else 0
            if delta < 0 and not allow_negative and current + delta < 0:
                raise InsufficientFunds(current, -delta)
            new_balance = max(0, min(MAX_CHIPS, current + delta))
            await conn.execute(
                "UPDATE players SET balance = ? WHERE guild_id = ? AND user_id = ?",
                (new_balance, guild_id, user_id),
            )
            await self._record(
                conn, guild_id, user_id,
                kind=kind, delta=new_balance - current, balance_after=new_balance, game=game, note=note,
            )
        return new_balance

    async def place_wager(self, guild_id: int, user_id: int, stake: int, game: str, detail: str | None = None) -> Wager:
        """Atomically take ``stake`` out of a balance, or refuse the bet."""
        stake = int(stake)
        if stake <= 0:
            raise InsufficientFunds(await self.balance(guild_id, user_id), stake)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE players SET balance = balance - ? "
                "WHERE guild_id = ? AND user_id = ? AND balance >= ?",
                (stake, guild_id, user_id, stake),
            )
            if cursor.rowcount != 1:
                # Either the player has no row yet or cannot cover the stake.
                row = await (await conn.execute(
                    "SELECT balance FROM players WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
                )).fetchone()
                raise InsufficientFunds(int(row["balance"]) if row else 0, stake)
            row = await (await conn.execute(
                "SELECT balance FROM players WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )).fetchone()
            balance_after = int(row["balance"])
        return Wager(guild_id, user_id, stake, game, balance_after, detail)

    async def settle(
        self,
        wager: Wager,
        payout: int,
        *,
        outcome: str | None = None,
        note: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Settlement:
        """Credit a wager's payout and fold the round into stats in one write.

        ``payout`` is the **total** returned to the player, stake included:
        zero for a loss, the stake for a push, more for a win.
        """
        if wager.settled:
            raise RuntimeError("wager already settled")
        # Claim the wager *before* the first await. A coroutine only yields at
        # an await, so checking and setting in the same synchronous block is
        # what makes this a real guard: two callers racing to settle the same
        # round (a button click landing at the same moment as a timeout, say)
        # cannot both get past this line and pay the round out twice.
        wager.settled = True
        payout = max(0, int(payout))
        net = payout - wager.stake
        resolved = outcome or ("win" if net > 0 else "push" if net == 0 else "lose")
        now = utcnow()
        periods = period_keys(now)

        try:
            return await self._settle_locked(wager, payout, net, resolved, note, extra, periods)
        except BaseException:
            # The round did not land, so release the claim and let the caller
            # retry rather than stranding the stake.
            wager.settled = False
            raise

    async def _settle_locked(
        self,
        wager: Wager,
        payout: int,
        net: int,
        resolved: str,
        note: str | None,
        extra: dict[str, Any] | None,
        periods: dict[str, str],
    ) -> Settlement:
        async with self.db.transaction() as conn:
            row = await (await conn.execute(
                "SELECT balance FROM players WHERE guild_id = ? AND user_id = ?",
                (wager.guild_id, wager.user_id),
            )).fetchone()
            current = int(row["balance"]) if row else 0
            balance = min(MAX_CHIPS, current + payout)
            await conn.execute(
                "UPDATE players SET balance = ? WHERE guild_id = ? AND user_id = ?",
                (balance, wager.guild_id, wager.user_id),
            )
            await self._record(
                conn, wager.guild_id, wager.user_id,
                kind="game", game=wager.game, stake=wager.stake,
                delta=net, balance_after=balance, note=note or resolved,
            )
            # Stats. streak counts consecutive wins; a push leaves it alone,
            # which is how the rest of the industry counts it.
            await conn.execute(
                """
                UPDATE stats SET
                    games = games + 1,
                    wins = wins + ?,
                    losses = losses + ?,
                    pushes = pushes + ?,
                    wagered = MIN(?, wagered + ?),
                    net = net + ?,
                    biggest_win = MAX(biggest_win, ?),
                    biggest_loss = MAX(biggest_loss, ?),
                    streak = CASE WHEN ? > 0 THEN streak + 1 WHEN ? < 0 THEN 0 ELSE streak END,
                    best_streak = MAX(best_streak, CASE WHEN ? > 0 THEN streak + 1 ELSE streak END),
                    last_game = ?,
                    last_stake = ?,
                    last_bet_detail = ?
                 WHERE guild_id = ? AND user_id = ?
                """,
                (
                    1 if net > 0 else 0,
                    1 if net < 0 else 0,
                    1 if net == 0 else 0,
                    MAX_CHIPS, wager.stake,
                    net,
                    max(0, net),
                    max(0, -net),
                    net, net, net,
                    wager.game,
                    wager.stake,
                    wager.detail,
                    wager.guild_id, wager.user_id,
                ),
            )
            await conn.execute(
                "INSERT INTO game_plays (guild_id, user_id, game, plays, wagered, net) VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT (guild_id, user_id, game) DO UPDATE SET "
                "plays = plays + 1, wagered = wagered + excluded.wagered, net = net + excluded.net",
                (wager.guild_id, wager.user_id, wager.game, wager.stake, net),
            )
            for kind, key in periods.items():
                await self._bump_challenge(
                    conn, wager.guild_id, wager.user_id, kind, key,
                    games=1, wagered=wager.stake, wins=1 if net > 0 else 0,
                )
        return Settlement(
            stake=wager.stake, payout=payout, net=net, balance=balance,
            outcome=resolved, game=wager.game, extra=extra or {},
        )

    async def refund(self, wager: Wager, reason: str = "cancelled") -> int:
        """Hand a stake back without recording a game (used for cancellations)."""
        if wager.settled:
            return await self.balance(wager.guild_id, wager.user_id)
        wager.settled = True
        return await self.adjust(
            wager.guild_id, wager.user_id, wager.stake,
            kind="refund", game=wager.game, note=reason,
        )

    async def transfer(self, guild_id: int, sender: int, recipient: int, amount: int) -> tuple[int, int]:
        """Move chips between two players atomically."""
        amount = int(amount)
        if amount <= 0:
            raise InsufficientFunds(await self.balance(guild_id, sender), amount)
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE players SET balance = balance - ? "
                "WHERE guild_id = ? AND user_id = ? AND balance >= ?",
                (amount, guild_id, sender, amount),
            )
            if cursor.rowcount != 1:
                row = await (await conn.execute(
                    "SELECT balance FROM players WHERE guild_id = ? AND user_id = ?", (guild_id, sender)
                )).fetchone()
                raise InsufficientFunds(int(row["balance"]) if row else 0, amount)
            await conn.execute(
                "UPDATE players SET balance = MIN(?, balance + ?) WHERE guild_id = ? AND user_id = ?",
                (MAX_CHIPS, amount, guild_id, recipient),
            )
            sender_balance = int((await (await conn.execute(
                "SELECT balance FROM players WHERE guild_id = ? AND user_id = ?", (guild_id, sender)
            )).fetchone())["balance"])
            recipient_row = await (await conn.execute(
                "SELECT balance FROM players WHERE guild_id = ? AND user_id = ?", (guild_id, recipient)
            )).fetchone()
            recipient_balance = int(recipient_row["balance"]) if recipient_row else 0
            await self._record(
                conn, guild_id, sender, kind="transfer", delta=-amount,
                balance_after=sender_balance, note=f"to {recipient}",
            )
            await self._record(
                conn, guild_id, recipient, kind="transfer", delta=amount,
                balance_after=recipient_balance, note=f"from {sender}",
            )
        return sender_balance, recipient_balance

    # -- faucets -----------------------------------------------------------

    async def claim_daily(self, guild_id: int, user_id: int, settings: dict[str, Any]) -> tuple[int, int]:
        """Claim the daily reward. Returns ``(amount, streak)``.

        The streak is a calendar-day streak in UTC, so the next claim unlocks
        at UTC midnight rather than 24 hours after the last one. The old bot
        told players both things at once depending on which screen they read.
        """
        now = utcnow()
        today = now.date()
        async with self.db.transaction() as conn:
            row = await (await conn.execute(
                "SELECT balance, last_daily_date, daily_streak FROM players WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )).fetchone()
            last_claim = row["last_daily_date"] if row else None
            if last_claim == today.isoformat():
                tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
                raise OnCooldown("daily reward", tomorrow - now)

            streak = int(row["daily_streak"]) if row else 0
            if last_claim:
                try:
                    previous = date.fromisoformat(last_claim)
                except ValueError:
                    previous = None
                streak = streak + 1 if previous and today == previous + timedelta(days=1) else 1
            else:
                streak = 1

            base = int(settings["daily_amount"])
            bonus = float(settings.get("daily_streak_bonus", 0.10))
            amount = int(base * min(1.0 + max(0, streak - 1) * bonus, 2.0))
            balance = min(MAX_CHIPS, int(row["balance"] if row else 0) + amount)
            await conn.execute(
                "UPDATE players SET balance = ?, last_daily = ?, last_daily_date = ?, daily_streak = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (balance, iso(now), today.isoformat(), streak, guild_id, user_id),
            )
            await self._record(
                conn, guild_id, user_id, kind="daily", delta=amount,
                balance_after=balance, note=f"day {streak} streak",
            )
        return amount, streak

    async def work(self, guild_id: int, user_id: int, settings: dict[str, Any]) -> tuple[int, int, float]:
        """Work a shift. Returns ``(earned, balance, multiplier)``."""
        now = utcnow()
        cooldown = timedelta(seconds=float(settings["work_cooldown"]))
        async with self.db.transaction() as conn:
            row = await (await conn.execute(
                "SELECT balance, last_work, work_level FROM players WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )).fetchone()
            last = parse_iso(row["last_work"]) if row else None
            if last is not None and now - last < cooldown:
                raise OnCooldown("next shift", cooldown - (now - last))

            level = int(row["work_level"]) if row else 0
            step = float(settings["upgrade_multiplier_step"])
            multiplier = 1.0 + step * level
            base = random.randint(int(settings["work_min"]), int(settings["work_max"]))
            earned = int(base * multiplier)
            balance = min(MAX_CHIPS, int(row["balance"] if row else 0) + earned)
            await conn.execute(
                "UPDATE players SET balance = ?, last_work = ? WHERE guild_id = ? AND user_id = ?",
                (balance, iso(now), guild_id, user_id),
            )
            await self._record(
                conn, guild_id, user_id, kind="work", delta=earned,
                balance_after=balance, note=f"shift at x{multiplier:g}",
            )
            for kind, key in period_keys(now).items():
                await self._bump_challenge(conn, guild_id, user_id, kind, key, work=1)
        return earned, balance, multiplier

    async def buy_upgrade(self, guild_id: int, user_id: int, settings: dict[str, Any]) -> tuple[dict[str, Any], int]:
        """Buy the next work upgrade. Returns ``(upgrade, new_balance)``."""
        async with self.db.transaction() as conn:
            row = await (await conn.execute(
                "SELECT balance, work_level FROM players WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )).fetchone()
            balance = int(row["balance"]) if row else 0
            level = int(row["work_level"]) if row else 0
            upgrade = describe_upgrade(settings, level)
            if balance < upgrade["cost"]:
                raise InsufficientFunds(balance, upgrade["cost"])
            balance -= upgrade["cost"]
            await conn.execute(
                "UPDATE players SET balance = ?, work_level = ? WHERE guild_id = ? AND user_id = ?",
                (balance, level + 1, guild_id, user_id),
            )
            await self._record(
                conn, guild_id, user_id, kind="upgrade", delta=-upgrade["cost"],
                balance_after=balance, note=upgrade["name"],
            )
        return upgrade, balance

    # -- challenges --------------------------------------------------------

    async def _bump_challenge(
        self, conn: Any, guild_id: int, user_id: int, kind: str, key: str,
        *, games: int = 0, wagered: int = 0, wins: int = 0, work: int = 0,
    ) -> None:
        """Add progress, resetting the row when the period rolls over."""
        await conn.execute(
            """
            INSERT INTO challenge_progress (guild_id, user_id, period_kind, period_key, games, wagered, wins, work, claimed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT (guild_id, user_id, period_kind) DO UPDATE SET
                period_key = excluded.period_key,
                games   = CASE WHEN challenge_progress.period_key = excluded.period_key THEN challenge_progress.games + excluded.games ELSE excluded.games END,
                wagered = CASE WHEN challenge_progress.period_key = excluded.period_key THEN challenge_progress.wagered + excluded.wagered ELSE excluded.wagered END,
                wins    = CASE WHEN challenge_progress.period_key = excluded.period_key THEN challenge_progress.wins + excluded.wins ELSE excluded.wins END,
                work    = CASE WHEN challenge_progress.period_key = excluded.period_key THEN challenge_progress.work + excluded.work ELSE excluded.work END,
                claimed = CASE WHEN challenge_progress.period_key = excluded.period_key THEN challenge_progress.claimed ELSE 0 END
            """,
            (guild_id, user_id, kind, key, games, wagered, wins, work),
        )

    async def challenge(self, guild_id: int, user_id: int, kind: str) -> dict[str, Any]:
        key = period_keys()[kind]
        row = await self.db.fetchone(
            "SELECT * FROM challenge_progress WHERE guild_id = ? AND user_id = ? AND period_kind = ?",
            (guild_id, user_id, kind),
        )
        if row is None or row["period_key"] != key:
            return {"period_key": key, "games": 0, "wagered": 0, "wins": 0, "work": 0, "claimed": 0}
        return dict(row)

    async def claim_challenge(self, guild_id: int, user_id: int, kind: str, reward: int, name: str) -> int:
        """Mark a challenge claimed and pay it, refusing a second claim."""
        key = period_keys()[kind]
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE challenge_progress SET claimed = 1 "
                "WHERE guild_id = ? AND user_id = ? AND period_kind = ? AND period_key = ? AND claimed = 0",
                (guild_id, user_id, kind, key),
            )
            if cursor.rowcount != 1:
                raise AlreadyClaimed(kind)
            row = await (await conn.execute(
                "SELECT balance FROM players WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )).fetchone()
            balance = min(MAX_CHIPS, int(row["balance"] if row else 0) + reward)
            await conn.execute(
                "UPDATE players SET balance = ? WHERE guild_id = ? AND user_id = ?",
                (balance, guild_id, user_id),
            )
            await self._record(
                conn, guild_id, user_id, kind="challenge", delta=reward,
                balance_after=balance, note=name,
            )
        return balance

    # -- leaderboards and history ------------------------------------------

    async def leaderboard(self, guild_id: int, metric: Metric = "balance", limit: int = 10) -> list[dict[str, Any]]:
        table, column = METRIC_COLUMNS[metric]
        if table == "players":
            sql = (
                f"SELECT user_id, {column} AS value FROM players "
                "WHERE guild_id = ? ORDER BY value DESC LIMIT ?"
            )
        else:
            sql = (
                f"SELECT s.user_id AS user_id, s.{column} AS value FROM stats s "
                "WHERE s.guild_id = ? ORDER BY value DESC LIMIT ?"
            )
        rows = await self.db.fetchall(sql, (guild_id, limit))
        return [dict(row) for row in rows]

    async def windowed_leaderboard(
        self, guild_id: int, metric: Literal["wagered", "net"], since: datetime, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Top players over a time window, aggregated by SQLite.

        The old version fetched every player's full transaction list into
        Python and summed it per request. This is one indexed query.
        """
        column = "SUM(stake)" if metric == "wagered" else "SUM(delta)"
        rows = await self.db.fetchall(
            f"""
            SELECT user_id,
                   {column} AS value,
                   SUM(stake) AS staked,
                   SUM(delta) AS delta
              FROM ledger
             WHERE guild_id = ? AND created_at >= ? AND kind = 'game'
             GROUP BY user_id
             ORDER BY value DESC
             LIMIT ?
            """,
            (guild_id, iso(since), limit),
        )
        return [dict(row) for row in rows]

    async def history(self, guild_id: int, user_id: int, limit: int = 8, offset: int = 0) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM ledger WHERE guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
            (guild_id, user_id, limit, offset),
        )
        return [dict(row) for row in rows]

    async def history_count(self, guild_id: int, user_id: int) -> int:
        return int(await self.db.fetchval(
            "SELECT COUNT(*) FROM ledger WHERE guild_id = ? AND user_id = ?", (guild_id, user_id), 0
        ))

    async def wagered_since(self, guild_id: int, user_id: int, since: datetime) -> int:
        return int(await self.db.fetchval(
            "SELECT COALESCE(SUM(stake), 0) FROM ledger "
            "WHERE guild_id = ? AND user_id = ? AND kind = 'game' AND created_at >= ?",
            (guild_id, user_id, iso(since)), 0,
        ))

    async def guild_totals(self, guild_id: int) -> dict[str, Any]:
        row = await self.db.fetchone(
            """
            SELECT COUNT(*) AS players,
                   COALESCE(SUM(p.balance), 0) AS chips,
                   COALESCE(SUM(s.games), 0) AS games,
                   COALESCE(SUM(s.wagered), 0) AS wagered,
                   COALESCE(SUM(s.net), 0) AS net,
                   COALESCE(MAX(s.biggest_win), 0) AS biggest_win
              FROM players p
              LEFT JOIN stats s ON s.guild_id = p.guild_id AND s.user_id = p.user_id
             WHERE p.guild_id = ?
            """,
            (guild_id,),
        )
        totals = dict(row) if row else {}
        popular = await self.db.fetchall(
            "SELECT game, SUM(plays) AS plays, SUM(wagered) AS wagered, SUM(net) AS net "
            "FROM game_plays WHERE guild_id = ? GROUP BY game ORDER BY plays DESC",
            (guild_id,),
        )
        totals["games_by_popularity"] = [dict(row) for row in popular]
        return totals

    # -- admin -------------------------------------------------------------

    async def set_balance(self, guild_id: int, user_id: int, amount: int, note: str = "admin set") -> int:
        amount = max(0, min(MAX_CHIPS, int(amount)))
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE players SET balance = ? WHERE guild_id = ? AND user_id = ?",
                (amount, guild_id, user_id),
            )
            await self._record(
                conn, guild_id, user_id, kind="admin", delta=0, balance_after=amount, note=note
            )
        return amount

    async def set_work_level(self, guild_id: int, user_id: int, level: int) -> int:
        level = max(0, int(level))
        await self.db.execute(
            "UPDATE players SET work_level = ? WHERE guild_id = ? AND user_id = ?",
            (level, guild_id, user_id),
        )
        return level

    async def clear_cooldowns(self, guild_id: int, user_id: int) -> None:
        await self.db.execute(
            "UPDATE players SET last_daily = NULL, last_daily_date = NULL, last_work = NULL "
            "WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    async def reset_player(self, guild_id: int, user_id: int, starting_balance: int) -> int:
        """Wipe a player's economy back to new, keeping self-exclusion intact.

        Self-exclusion deliberately survives a reset: it exists to protect
        someone from themselves, so neither they nor an admin should be able
        to clear it early by wiping the account.
        """
        now = iso(utcnow())
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE players SET balance = ?, work_level = 0, last_daily = NULL, "
                "last_daily_date = NULL, daily_streak = 0, last_work = NULL "
                "WHERE guild_id = ? AND user_id = ?",
                (int(starting_balance), guild_id, user_id),
            )
            for table in ("stats", "challenge_progress", "game_plays", "inventory", "achievements"):
                await conn.execute(f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            await conn.execute("INSERT INTO stats (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id))
            await conn.execute(
                "UPDATE progression SET xp = 0, level = 1, prestige = 0, title = NULL, badge = NULL, "
                "casino_ban_until = NULL, daily_wager_limit = NULL, suspicion = 0 "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await conn.execute("DELETE FROM ledger WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
            await self._record(
                conn, guild_id, user_id, kind="admin", delta=0,
                balance_after=int(starting_balance), note="full reset",
            )
            await conn.execute("UPDATE meta SET value = ? WHERE key = 'last_reset'", (now,))
        return int(starting_balance)

    async def player_ids(self, guild_id: int) -> list[int]:
        rows = await self.db.fetchall("SELECT user_id FROM players WHERE guild_id = ?", (guild_id,))
        return [int(row["user_id"]) for row in rows]


class AlreadyClaimed(Exception):
    """Raised when a challenge reward has already been taken this period."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"{kind} challenge already claimed")
        self.kind = kind


def merge_wagers(primary: Wager, extra: Wager) -> Wager:
    """Fold a top-up debit into an existing wager.

    Doubling down needs a second debit, but the round should still produce a
    *single* ledger row and a single stats entry — a doubled hand is one hand,
    not two. Both debits have already happened, so folding the stakes together
    and marking the extra as settled leaves the balance correct and the
    history readable.
    """
    if (primary.guild_id, primary.user_id) != (extra.guild_id, extra.user_id):
        raise ValueError("cannot merge wagers belonging to different players")
    if primary.settled or extra.settled:
        raise ValueError("cannot merge a settled wager")
    primary.stake += extra.stake
    extra.settled = True
    return primary


UPGRADE_NAMES: Sequence[str] = (
    "Part-time Job", "Full-time Job", "Shift Lead", "Floor Manager",
    "Pit Boss", "Operations Manager", "Regional Director", "Vice President", "Chief Executive",
)


def describe_upgrade(settings: dict[str, Any], level: int) -> dict[str, Any]:
    """Cost and effect of the next work upgrade.

    One function so the shop screen and the purchase can never disagree about
    the price — the bug class the old bot avoided by accident and documented
    at length.
    """
    next_level = level + 1
    base = float(settings["upgrade_base_cost"])
    growth = float(settings["upgrade_cost_growth"])
    step = float(settings["upgrade_multiplier_step"])
    name = (
        UPGRADE_NAMES[next_level - 1]
        if next_level <= len(UPGRADE_NAMES)
        else f"Executive Tier {next_level - len(UPGRADE_NAMES)}"
    )
    return {
        "level": next_level,
        "name": name,
        "cost": max(1, int(round(base * (growth**level)))),
        "multiplier": 1.0 + step * next_level,
        "step": step,
    }


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))
