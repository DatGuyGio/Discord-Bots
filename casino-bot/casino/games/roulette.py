"""European roulette: a single zero, 37 pockets.

Bets are described declaratively — a label, the set of pockets that win, and
the payout — so the resolver, the help text, the bet board UI, and the tests
all read from the same table. The old bot spelled out payouts in a long
if/elif chain, which is exactly where a 2:1 quietly becomes a 3:1.
"""

from __future__ import annotations

from dataclasses import dataclass

POCKETS = tuple(range(0, 37))

RED = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})
BLACK = frozenset(set(range(1, 37)) - RED)
GREEN = frozenset({0})


def pocket_color(number: int) -> str:
    if number in GREEN:
        return "green"
    return "red" if number in RED else "black"


COLOR_EMOJI = {"red": "🔴", "black": "⚫", "green": "🟢"}


@dataclass(frozen=True, slots=True)
class BetKind:
    """One wagerable proposition on the table."""

    key: str
    label: str
    winning: frozenset[int]
    payout: int  # profit per chip staked, i.e. the ":1" in "35:1"
    group: str
    aliases: tuple[str, ...] = ()

    @property
    def probability(self) -> float:
        return len(self.winning) / len(POCKETS)

    @property
    def house_edge(self) -> float:
        """Expected loss per chip. Every European bet sits at 1/37 = 2.70%."""
        return 1.0 - self.probability * (self.payout + 1)

    def wins(self, result: int) -> bool:
        return result in self.winning


def _dozen(index: int) -> frozenset[int]:
    start = 1 + (index - 1) * 12
    return frozenset(range(start, start + 12))


def _column(index: int) -> frozenset[int]:
    return frozenset(n for n in range(1, 37) if n % 3 == index % 3)


OUTSIDE_BETS: tuple[BetKind, ...] = (
    BetKind("red", "Red", RED, 1, "colour", ("r",)),
    BetKind("black", "Black", BLACK, 1, "colour", ("b",)),
    BetKind("odd", "Odd", frozenset(n for n in range(1, 37) if n % 2), 1, "parity"),
    BetKind("even", "Even", frozenset(n for n in range(1, 37) if n % 2 == 0), 1, "parity"),
    BetKind("low", "Low (1-18)", frozenset(range(1, 19)), 1, "half", ("1-18", "small")),
    BetKind("high", "High (19-36)", frozenset(range(19, 37)), 1, "half", ("19-36", "big")),
    BetKind("dozen1", "1st dozen (1-12)", _dozen(1), 2, "dozen", ("1st12", "d1", "1-12")),
    BetKind("dozen2", "2nd dozen (13-24)", _dozen(2), 2, "dozen", ("2nd12", "d2", "13-24")),
    BetKind("dozen3", "3rd dozen (25-36)", _dozen(3), 2, "dozen", ("3rd12", "d3", "25-36")),
    BetKind("column1", "Column 1", _column(1), 2, "column", ("col1", "c1")),
    BetKind("column2", "Column 2", _column(2), 2, "column", ("col2", "c2")),
    BetKind("column3", "Column 3", _column(0), 2, "column", ("col3", "c3")),
)

_BY_KEY: dict[str, BetKind] = {}
for _bet in OUTSIDE_BETS:
    _BY_KEY[_bet.key] = _bet
    for _alias in _bet.aliases:
        _BY_KEY[_alias] = _bet


def straight_up(number: int) -> BetKind:
    """A single-number bet, 35:1."""
    if number not in POCKETS:
        raise ValueError("roulette numbers run from 0 to 36")
    return BetKind(str(number), f"Straight up {number}", frozenset({number}), 35, "straight")


def parse_bet(raw: str) -> BetKind | None:
    """Turn user input into a :class:`BetKind`, or ``None`` if it is nonsense.

    Accepts numbers (``17``), names (``black``), aliases (``1st12``), and
    inclusive ranges the table actually offers (``19-36``).
    """
    if raw is None:
        return None
    text = raw.strip().lower().replace(" ", "").replace("_", "")
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        return straight_up(number) if number in POCKETS else None
    return _BY_KEY.get(text)


def bet_help_lines() -> list[str]:
    """Human-readable payout table, grouped, for the help embed."""
    lines = ["`0`-`36` — straight up, pays **35:1**"]
    seen: set[str] = set()
    for bet in OUTSIDE_BETS:
        if bet.group in seen:
            continue
        seen.add(bet.group)
        peers = [b for b in OUTSIDE_BETS if b.group == bet.group]
        keys = " / ".join(f"`{b.key}`" for b in peers)
        lines.append(f"{keys} — pays **{bet.payout}:1**")
    return lines


@dataclass(slots=True)
class Spin:
    """The result of one spin, plus how a bet fared against it."""

    number: int
    color: str
    won: bool
    payout: int  # total chips returned, stake included

    @property
    def emoji(self) -> str:
        return COLOR_EMOJI[self.color]

    def describe(self) -> str:
        return f"**{self.number}** {self.emoji} ({self.color})"


def spin(bet: BetKind, stake: int, roll: int) -> Spin:
    """Resolve ``bet`` against an already-drawn pocket.

    Drawing is kept outside this function so tests can walk all 37 pockets
    deterministically and the UI can animate before revealing.
    """
    won = bet.wins(roll)
    return Spin(
        number=roll,
        color=pocket_color(roll),
        won=won,
        payout=stake * (bet.payout + 1) if won else 0,
    )


def neighbours(number: int, span: int = 2) -> list[int]:
    """Pockets physically next to ``number`` on the wheel, for flavour text."""
    order = [
        0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10,
        5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26,
    ]
    index = order.index(number)
    return [order[(index + offset) % len(order)] for offset in range(-span, span + 1) if offset]
