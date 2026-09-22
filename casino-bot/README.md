# Casino bot

A Discord casino, economy and moderation bot. Slash commands throughout,
interactive game surfaces, an auditable chip ledger, and provably fair
outcomes. Chips are virtual, scoped per server, and cannot be bought.

This is a rebuild of an earlier single-file bot (`roulette_bot.py`). What
changed and why is in the pull request description and the accompanying
explainer document; the short version is below.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add your bot token
python -m casino
```

The bot needs the **Message Content** and **Server Members** privileged
intents (Developer Portal → your app → Bot → Privileged Gateway Intents), and
the `bot` plus `applications.commands` scopes on its invite link.

Slash commands sync globally on first boot, which can take up to an hour to
appear. While developing, set `CASINO_DEV_GUILD` to one server id for an
instant sync, or run `!sync` as the owner.

## Migrating from the JSON version

```bash
python -m casino.db.migrate --source /path/to/old/bot --db casino.sqlite3
```

This reads `balances.json`, `casino_economy.json`, `bot_features.json`,
`casino_progress.json` and `moderation.json`, and writes them into SQLite.
It is idempotent, it never deletes the JSON files, and it prints a report of
what it imported, clamped, or skipped.

Two things it deliberately changes:

- Values beyond a trillion chips are clamped. The live data contained a
  999,999,999,999,999,999,999,999,999 chip bet, which is the fossil of a
  missing bet-limit check.
- `max_bet: 0`, which used to mean "no limit", becomes the default ceiling.
  Carrying it across would carry the exploit across with it.

## Games and odds

Every house game derives its payout from one number, so the edge is a single
knob (`/config set rtp 0.97`) rather than a dozen scattered multipliers.

| Game | Return to player | How it plays |
| --- | --- | --- |
| Blackjack | ~99.5% with basic strategy | Six-deck shoe, dealer stands on 17, blackjack pays 3:2, double / split / insurance |
| Roulette | 97.3% (single zero) | Bet board or typed bets, 35:1 straight up |
| Crash | 97% (configurable) | Multiplier climbs, one button to jump off |
| Mines | 97% (configurable) | Clickable grid, multiplier rises per safe tile, bank any time |
| Coinflip / Dice | 97% (configurable) | Instant, with one-click replay |
| Slots | 94.8% | Eight symbols, triple sevens pay x1500 |
| Duel | 100% (no house cut) | Player versus player, winner takes the pot |

`/paytable` shows the slots table with its measured return;
`tests/test_games.py` asserts it by enumerating all 512 reel combinations.

## Provable fairness

Each round generates a secret seed, publishes `sha256(seed)` in the result
footer, derives the outcome from `HMAC-SHA256(seed, "client:nonce")`, and
reveals the seed afterwards. `/verify` re-hashes a revealed seed in front of
the player. The bot therefore cannot pick a seed after seeing a bet, and
cannot deny the seed it committed to. Randomness comes from the operating
system's cryptographic source, not a clock-seeded generator.

## Commands

Everything works as `/command` and as `!command`. `/help` is generated from
the live command tree, so it cannot drift.

- **Casino** `/casino` `/roulette` `/blackjack` `/slots` `/mines` `/crash`
  `/coinflip` `/dice` `/duel` `/paytable` `/verify`
- **Economy** `/balance` `/daily` `/work` `/jobs` `/upgrade` `/give`
  `/history` `/timers`
- **Progress** `/profile` `/achievements` `/challenges` `/shop` `/prestige`
  `/leaderboard`
- **Responsible play** `/wagerlimit` `/selfexclude` — both are one-way, and
  neither staff nor an account reset can lift them early
- **Moderation** `/warn` `/warnings` `/timeout` `/kick` `/ban` `/unban`
  `/purge` `/slowmode` `/lock` `/nick`
- **Tickets** `/ticketsetup` `/ticketpanel` `/ticketadd`
- **Admin** `/config` `/gametoggle` `/logconfig` `/bank` `/resetplayer`
  `/reseteconomy` `/casinoban` `/maintenance` `/event` `/analytics`

Stakes accept `500`, `10k`, `2.5m`, `half`, `25%` and `all` everywhere,
including inside the button modals.

## Layout

```
casino/
  config.py          process settings and per-guild defaults
  core/
    bot.py           the client: wiring, lifecycle, one error renderer
    guard.py         the single funnel every bet passes through
    rounds.py        the single exit every round passes through
    amounts.py       "10k", "half", "25%" -> chips
    checks.py        staff permissions and moderation hierarchy checks
    errors.py        one error type per player-visible rejection
  db/
    schema.sql       tables; players.balance is the only source of truth
    database.py      connection and the transaction primitive
    economy.py       balances, wagers, settlements, ledger, leaderboards
    guilds.py        per-guild settings, game switches, log switches
    progression.py   levels, cosmetics, achievements, play limits
    support.py       warnings and tickets
    migrate.py       one-shot JSON import
  games/             pure logic: no Discord, no database, fully testable
  ui/                theme, reusable components, game surfaces, the hub
  cogs/              one feature area per file
tests/               296 tests, no network or token needed
```

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # ~25 seconds
python -m pyflakes casino tests
```

The suite covers game maths (including exact return-to-player by
enumeration), the ledger's concurrency invariants, the bet funnel's rejection
rules, the JSON import against real saved data, and the command handlers
themselves driven through stand-in interactions. None of it needs a token.

## Design notes

**Wagers are atomic.** Taking a bet is one conditional `UPDATE` inside
`BEGIN IMMEDIATE`:

```sql
UPDATE players SET balance = balance - :stake
 WHERE guild_id = :g AND user_id = :u AND balance >= :stake
```

Zero rows changed means the player could not afford it. Two clicks arriving
together are serialised, so the same chips cannot be spent twice and a
balance cannot go negative. `tests/test_economy.py` fires ten simultaneous
200-chip bets at a 1,000 chip balance and asserts that exactly five succeed.

**The ledger is append-only.** Every chip movement writes one row with the
resulting balance, so `/history` is a query, windowed leaderboards are a
single indexed aggregate, and any balance can be rebuilt from its history.

**One funnel in, one funnel out.** `RoundGuard.open` performs every check —
maintenance, game switch, casino ban, self-exclusion, daily wager limit, bet
range, affordability — and is the only way to take a stake.
`rounds.conclude` credits the payout and applies XP, achievements and logging.
A new game inherits all of it and cannot forget any of it.

**Chips are virtual.** There is no purchase path, and there never should be.
`/selfexclude` exists and is deliberately impossible to undo.
