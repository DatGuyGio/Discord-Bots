"""One-shot import of the old JSON files into SQLite.

Run it once, from the directory holding the old bot's data::

    python -m casino.db.migrate --source /path/to/old/bot --db casino.sqlite3

It is idempotent: rows that already exist are left alone, so re-running
after a partial import is safe. Nothing is deleted from the JSON files.

The import also *sanitises*. The live data contains a 999999999999999999999999999
chip blackjack bet, which is how one account ended up with a ``total_wagered``
of 10^27 and a ``net_profit`` of -10^27. Those numbers break formatting, make
every leaderboard meaningless, and are the fossil of a bug (the button flows
never checked the configured maximum bet). Anything above
:data:`casino.config.MAX_CHIPS` is clamped and reported.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from casino.config import GAME_KEYS, GUILD_DEFAULTS, MAX_CHIPS
from casino.db.database import Database
from casino.db.economy import iso, utcnow

log = logging.getLogger("casino.migrate")

#: Old cosmetic ids -> new shop ids. Anything unmapped is reported, not dropped
#: silently, so nobody quietly loses a purchase.
LEGACY_ITEMS = {
    "title_winner": "title_rookie",
    "title_highroller": "title_highroller",
    "title_whale": "title_whale",
    "cosmetic_gold": "badge_gold",
    "cosmetic_neon": "badge_neon",
    "cosmetic_royal": "badge_royal",
}

#: Settings that carry across unchanged.
LEGACY_SETTINGS = (
    "starting_balance", "min_bet", "max_bet", "daily_amount",
    "work_min", "work_max", "work_cooldown",
    "upgrade_base_cost", "upgrade_cost_growth", "upgrade_multiplier_step",
)

LEGACY_FEATURES = (
    "welcome_channel", "welcome_message", "goodbye_channel", "goodbye_message",
    "logs_channel", "ticket_category", "ticket_support_role",
    "ticket_transcript_channel", "ticket_message", "event_name",
)


@dataclass
class Report:
    players: int = 0
    ledger_rows: int = 0
    guilds: int = 0
    warnings: int = 0
    tickets: int = 0
    progress: int = 0
    items: int = 0
    clamped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"players imported      {self.players}",
            f"ledger rows imported  {self.ledger_rows}",
            f"guild configs         {self.guilds}",
            f"progression rows      {self.progress}",
            f"cosmetics granted     {self.items}",
            f"warnings              {self.warnings}",
            f"tickets               {self.tickets}",
        ]
        if self.clamped:
            lines.append(f"clamped values        {len(self.clamped)}")
            lines.extend(f"  ! {entry}" for entry in self.clamped[:10])
        if self.skipped:
            lines.append(f"skipped entries       {len(self.skipped)}")
            lines.extend(f"  - {entry}" for entry in self.skipped[:10])
        return "\n".join(lines)


def _load(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read %s: %s", path.name, exc)
        return None


def _split_key(key: str) -> tuple[int, int] | None:
    """``"123:456"`` -> ``(guild_id, user_id)``; bare user keys return None."""
    if ":" not in key:
        return None
    guild, _, user = key.partition(":")
    if not (guild.isdigit() and user.isdigit()):
        return None
    return int(guild), int(user)


def _clamp(report: Report, label: str, value: Any, ceiling: int = MAX_CHIPS) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if abs(number) > ceiling:
        report.clamped.append(f"{label}: {number} -> {ceiling if number > 0 else -ceiling}")
        return ceiling if number > 0 else -ceiling
    return number


async def migrate(source: Path, db: Database) -> Report:
    report = Report()
    await _migrate_settings(source, db, report)
    await _migrate_players(source, db, report)
    await _migrate_progress(source, db, report)
    await _migrate_moderation(source, db, report)
    await db.set_meta("json_import_at", iso(utcnow()))
    await db.set_meta("json_import_source", str(source))
    return report


async def _migrate_settings(source: Path, db: Database, report: Report) -> None:
    economy = _load(source / "casino_economy.json") or {}
    features = _load(source / "bot_features.json") or {}
    guild_features = features.get("guilds", {}) if isinstance(features, dict) else {}

    guild_ids = {key for key in economy if key.isdigit()} | {key for key in guild_features if key.isdigit()}
    for raw_id in guild_ids:
        guild_id = int(raw_id)
        settings: dict[str, Any] = {}
        old_economy = economy.get(raw_id, {})
        for key in LEGACY_SETTINGS:
            if key in old_economy and key in GUILD_DEFAULTS:
                value = old_economy[key]
                # max_bet = 0 meant "unlimited" before; that is what let a
                # single bet reach 10^27, so it becomes a real ceiling now.
                if key == "max_bet" and not value:
                    report.skipped.append(f"guild {guild_id}: max_bet 0 (unlimited) -> default")
                    continue
                settings[key] = _clamp(report, f"guild {guild_id}.{key}", value) if isinstance(value, int) else value
        old_features = guild_features.get(raw_id, {})
        for key in LEGACY_FEATURES:
            if key in old_features and key in GUILD_DEFAULTS and old_features[key] is not None:
                settings[key] = old_features[key]
        if old_features.get("casino_maintenance"):
            settings["maintenance"] = 1

        if settings:
            payload = json.dumps(settings)
            await db.execute(
                "INSERT INTO guild_settings (guild_id, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id) DO NOTHING",
                (guild_id, payload, iso(utcnow())),
            )
            report.guilds += 1

        for game, enabled in (old_economy.get("enabled_games") or {}).items():
            if game in GAME_KEYS:
                await db.execute(
                    "INSERT INTO guild_games (guild_id, game, enabled) VALUES (?, ?, ?) "
                    "ON CONFLICT (guild_id, game) DO NOTHING",
                    (guild_id, game, int(bool(enabled))),
                )
        for kind, enabled in (old_features.get("logs_enabled") or {}).items():
            await db.execute(
                "INSERT INTO guild_logs (guild_id, kind, enabled) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id, kind) DO NOTHING",
                (guild_id, kind, int(bool(enabled))),
            )

    tickets = features.get("tickets", {}) if isinstance(features, dict) else {}
    for channel_id, ticket in tickets.items():
        if not str(channel_id).isdigit():
            continue
        await db.execute(
            "INSERT INTO tickets (channel_id, guild_id, user_id, category, claimed_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (channel_id) DO NOTHING",
            (
                int(channel_id), int(ticket.get("guild_id", 0)), int(ticket.get("user_id", 0)),
                ticket.get("category", "general"), ticket.get("claimed_by"),
                ticket.get("created_at") or iso(utcnow()),
            ),
        )
        report.tickets += 1


async def _migrate_players(source: Path, db: Database, report: Report) -> None:
    balances = _load(source / "balances.json") or {}
    for key, data in balances.items():
        parsed = _split_key(key)
        if parsed is None:
            # Pre-guild-scoping entries. The old bot already copied these into
            # each guild's namespace on startup, so the guild-scoped rows above
            # are the authoritative copy and this one is a duplicate.
            report.skipped.append(f"legacy global balance key {key}")
            continue
        guild_id, user_id = parsed
        label = f"{guild_id}/{user_id}"
        await db.execute(
            "INSERT INTO players (guild_id, user_id, balance, work_level, last_daily, last_daily_date, "
            "daily_streak, last_work, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO NOTHING",
            (
                guild_id, user_id,
                _clamp(report, f"{label}.balance", data.get("balance")),
                int(data.get("work_level") or 0),
                data.get("last_daily"), data.get("last_daily_date"),
                int(data.get("daily_streak") or 0), data.get("last_work"),
                iso(utcnow()),
            ),
        )
        await db.execute(
            "INSERT INTO stats (guild_id, user_id, games, wins, losses, wagered, net, biggest_win, "
            "biggest_loss, streak, best_streak, last_game, last_stake, last_bet_detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO NOTHING",
            (
                guild_id, user_id,
                int(data.get("games_played") or 0),
                int(data.get("games_won") or 0),
                int(data.get("games_lost") or 0),
                _clamp(report, f"{label}.total_wagered", data.get("total_wagered")),
                _clamp(report, f"{label}.net_profit", data.get("net_profit")),
                _clamp(report, f"{label}.biggest_win", data.get("biggest_win")),
                _clamp(report, f"{label}.biggest_loss", data.get("biggest_loss")),
                int(data.get("current_streak") or 0),
                int(data.get("best_streak") or 0),
                (data.get("last_game") or "").lower() or None,
                _clamp(report, f"{label}.last_bet", data.get("last_bet")),
                data.get("last_roulette_bet"),
            ),
        )
        report.players += 1

        now = iso(utcnow())
        for entry in data.get("transactions") or []:
            kind = str(entry.get("type") or "unknown").lower()
            is_game = "net" in entry
            stake = _clamp(report, f"{label}.tx.wagered", entry.get("wagered", 0))
            delta = _clamp(report, f"{label}.tx.delta", entry.get("net", entry.get("amount", 0)))
            await db.execute(
                "INSERT INTO ledger (guild_id, user_id, kind, game, stake, delta, balance_after, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    guild_id, user_id,
                    "game" if is_game else kind,
                    kind if is_game else None,
                    stake, delta, 0,
                    f"imported: {entry.get('note', '')}".strip(": "),
                    entry.get("timestamp") or now,
                ),
            )
            report.ledger_rows += 1
            if is_game:
                await db.execute(
                    "INSERT INTO game_plays (guild_id, user_id, game, plays, wagered, net) VALUES (?, ?, ?, 1, ?, ?) "
                    "ON CONFLICT (guild_id, user_id, game) DO UPDATE SET plays = plays + 1, "
                    "wagered = wagered + excluded.wagered, net = net + excluded.net",
                    (guild_id, user_id, kind, stake, delta),
                )

        for achievement in data.get("achievements") or []:
            await db.execute(
                "INSERT INTO achievements (guild_id, user_id, achievement_id, unlocked_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id, achievement_id) DO NOTHING",
                (guild_id, user_id, achievement, now),
            )

        for period, column in (("daily_challenge", "date"), ("weekly_challenge", "week")):
            block = data.get(period) or {}
            if not block.get(column):
                continue
            await db.execute(
                "INSERT INTO challenge_progress (guild_id, user_id, period_kind, period_key, games, wagered, "
                "wins, work, claimed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id, period_kind) DO NOTHING",
                (
                    guild_id, user_id, period.split("_")[0], block[column],
                    int(block.get("games") or 0),
                    _clamp(report, f"{label}.{period}.wagered", block.get("wagered")),
                    int(block.get("wins") or 0), int(block.get("work") or 0),
                    int(bool(block.get("claimed"))),
                ),
            )


async def _migrate_progress(source: Path, db: Database, report: Report) -> None:
    progress = _load(source / "casino_progress.json") or {}
    for key, data in progress.items():
        parsed = _split_key(key)
        if parsed is None:
            report.skipped.append(f"legacy global progress key {key}")
            continue
        guild_id, user_id = parsed
        await db.execute(
            "INSERT INTO progression (guild_id, user_id, xp, level, prestige, title, casino_ban_until, "
            "self_exclude_until, daily_wager_limit, suspicion) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, user_id) DO NOTHING",
            (
                guild_id, user_id,
                int(data.get("xp") or 0), max(1, int(data.get("level") or 1)),
                int(data.get("prestige") or 0),
                LEGACY_ITEMS.get(data.get("title") or "", None),
                data.get("casino_ban_until"), data.get("self_exclude_until"),
                data.get("daily_wager_limit"), int(data.get("suspicion") or 0),
            ),
        )
        report.progress += 1

        owned = list(data.get("owned_titles") or []) + list(data.get("owned_cosmetics") or [])
        for legacy_id in owned:
            new_id = LEGACY_ITEMS.get(legacy_id)
            if new_id is None:
                report.skipped.append(f"unknown cosmetic {legacy_id} for {guild_id}/{user_id}")
                continue
            await db.execute(
                "INSERT INTO inventory (guild_id, user_id, item_id, acquired_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id, item_id) DO NOTHING",
                (guild_id, user_id, new_id, iso(utcnow())),
            )
            report.items += 1


async def _migrate_moderation(source: Path, db: Database, report: Report) -> None:
    moderation = _load(source / "moderation.json") or {}
    log_channel = moderation.get("log_channel_id")
    warnings = moderation.get("warnings") or {}

    # Old warnings were global. Attach them to every guild the bot has data
    # for, because that is the closest honest reading of a global record.
    guild_ids = [
        int(row["guild_id"])
        for row in await db.fetchall("SELECT DISTINCT guild_id FROM players")
    ]
    for user_id, entries in warnings.items():
        if not str(user_id).isdigit():
            continue
        for guild_id in guild_ids:
            for entry in entries:
                await db.execute(
                    "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        guild_id, int(user_id), int(entry.get("moderator_id") or 0),
                        entry.get("reason") or "imported", entry.get("timestamp") or iso(utcnow()),
                    ),
                )
                report.warnings += 1

    if log_channel and guild_ids:
        report.skipped.append(
            f"global log channel {log_channel} not applied automatically; set it with /logconfig"
        )


async def _main(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = await Database(args.db).connect()
    try:
        report = await migrate(Path(args.source), db)
    finally:
        await db.close()
    print(report.render())


def main() -> None:
    parser = argparse.ArgumentParser(description="Import the old JSON data into SQLite.")
    parser.add_argument("--source", default=".", help="directory containing balances.json etc.")
    parser.add_argument("--db", default="casino.sqlite3", help="destination SQLite file")
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
