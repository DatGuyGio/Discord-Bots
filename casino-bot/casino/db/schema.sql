-- Schema for the casino bot. Applied on every boot; every statement is
-- CREATE ... IF NOT EXISTS so it doubles as the migration path.
--
-- Two shapes matter here:
--   * `players.balance` is the single source of truth for chips. Nothing else
--     stores a balance, so nothing else can disagree with it.
--   * `ledger` is append-only. Every chip that enters or leaves a balance
--     writes one row, which is what makes "how did I lose 40k last night"
--     and windowed leaderboards answerable with SQL instead of guesswork.

CREATE TABLE IF NOT EXISTS players (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    balance         INTEGER NOT NULL DEFAULT 0,
    work_level      INTEGER NOT NULL DEFAULT 0,
    last_daily      TEXT,
    last_daily_date TEXT,
    daily_streak    INTEGER NOT NULL DEFAULT 0,
    last_work       TEXT,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
) STRICT;

CREATE TABLE IF NOT EXISTS stats (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    games          INTEGER NOT NULL DEFAULT 0,
    wins           INTEGER NOT NULL DEFAULT 0,
    losses         INTEGER NOT NULL DEFAULT 0,
    pushes         INTEGER NOT NULL DEFAULT 0,
    wagered        INTEGER NOT NULL DEFAULT 0,
    net            INTEGER NOT NULL DEFAULT 0,
    biggest_win    INTEGER NOT NULL DEFAULT 0,
    biggest_loss   INTEGER NOT NULL DEFAULT 0,
    streak         INTEGER NOT NULL DEFAULT 0,
    best_streak    INTEGER NOT NULL DEFAULT 0,
    last_game      TEXT,
    last_stake     INTEGER NOT NULL DEFAULT 0,
    last_bet_detail TEXT,
    PRIMARY KEY (guild_id, user_id)
) STRICT;

-- Per-game breakdown, so "favourite game" is the game played most rather
-- than whatever happened to be played last (which is what it used to mean).
CREATE TABLE IF NOT EXISTS game_plays (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    game     TEXT    NOT NULL,
    plays    INTEGER NOT NULL DEFAULT 0,
    wagered  INTEGER NOT NULL DEFAULT 0,
    net      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, game)
) STRICT;

CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    kind          TEXT    NOT NULL,  -- game | daily | work | transfer | shop | admin | refund ...
    game          TEXT,
    stake         INTEGER NOT NULL DEFAULT 0,
    delta         INTEGER NOT NULL,  -- signed change to the balance
    balance_after INTEGER NOT NULL,
    note          TEXT,
    created_at    TEXT    NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_ledger_player ON ledger (guild_id, user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_window ON ledger (guild_id, created_at);

CREATE TABLE IF NOT EXISTS progression (
    guild_id           INTEGER NOT NULL,
    user_id            INTEGER NOT NULL,
    xp                 INTEGER NOT NULL DEFAULT 0,
    level              INTEGER NOT NULL DEFAULT 1,
    prestige           INTEGER NOT NULL DEFAULT 0,
    title              TEXT,
    badge              TEXT,
    casino_ban_until   TEXT,
    self_exclude_until TEXT,
    daily_wager_limit  INTEGER,
    suspicion          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
) STRICT;

CREATE TABLE IF NOT EXISTS inventory (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    item_id     TEXT    NOT NULL,
    acquired_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, item_id)
) STRICT;

CREATE TABLE IF NOT EXISTS achievements (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    achievement_id TEXT    NOT NULL,
    unlocked_at    TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, achievement_id)
) STRICT;

CREATE TABLE IF NOT EXISTS challenge_progress (
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    period_kind TEXT    NOT NULL,  -- daily | weekly
    period_key  TEXT    NOT NULL,  -- 2026-09-22 | 2026-W38
    games       INTEGER NOT NULL DEFAULT 0,
    wagered     INTEGER NOT NULL DEFAULT 0,
    wins        INTEGER NOT NULL DEFAULT 0,
    work        INTEGER NOT NULL DEFAULT 0,
    claimed     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, period_kind)
) STRICT;

-- Guild configuration is a JSON document validated against GUILD_DEFAULTS on
-- write. Adding a setting therefore needs no schema change.
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id   INTEGER PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS guild_games (
    guild_id INTEGER NOT NULL,
    game     TEXT    NOT NULL,
    enabled  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, game)
) STRICT;

CREATE TABLE IF NOT EXISTS guild_logs (
    guild_id INTEGER NOT NULL,
    kind     TEXT    NOT NULL,
    enabled  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, kind)
) STRICT;

CREATE TABLE IF NOT EXISTS warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason       TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings (guild_id, user_id, id DESC);

CREATE TABLE IF NOT EXISTS tickets (
    channel_id INTEGER PRIMARY KEY,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    category   TEXT    NOT NULL,
    claimed_by INTEGER,
    created_at TEXT    NOT NULL,
    closed_at  TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_tickets_owner ON tickets (guild_id, user_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
