"""Reading an amount of chips out of whatever the player typed.

The old bot accepted ``all`` in text commands but not in the bet modals, and
parsed with a bare ``int(...)``, so ``10k`` was an error and ``1,000`` worked
only sometimes. One parser, used by every entry point, fixes both.
"""

from __future__ import annotations

import re

from casino.core.errors import InvalidAmount

SUFFIXES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "t": 1_000_000_000_000}

_NUMBER = re.compile(r"^(\d+(?:\.\d+)?)([kmbt])?$")


def parse_amount(raw: str | int | None, *, balance: int) -> int:
    """Turn user input into a chip count.

    Accepts plain numbers with or without separators, ``k``/``m``/``b``/``t``
    suffixes, percentages, and the shorthands ``all``, ``half`` and ``max``.

    >>> parse_amount("2.5k", balance=10_000)
    2500
    >>> parse_amount("half", balance=900)
    450
    >>> parse_amount("25%", balance=1_000)
    250
    """
    if raw is None:
        raise InvalidAmount("nothing")
    if isinstance(raw, int):
        return raw

    text = raw.strip().lower().replace(",", "").replace("_", "").replace(" ", "")
    if not text:
        raise InvalidAmount(raw)

    if text in ("all", "allin", "max", "everything"):
        return balance
    if text in ("half", "50%"):
        return balance // 2
    if text in ("quarter", "25%"):
        return balance // 4

    if text.endswith("%"):
        try:
            share = float(text[:-1])
        except ValueError:
            raise InvalidAmount(raw) from None
        if not 0 < share <= 100:
            raise InvalidAmount(raw)
        return int(balance * share / 100)

    match = _NUMBER.match(text)
    if match is None:
        raise InvalidAmount(raw)
    value = float(match.group(1))
    if match.group(2):
        value *= SUFFIXES[match.group(2)]
    result = int(value)
    if result <= 0:
        raise InvalidAmount(raw)
    return result
