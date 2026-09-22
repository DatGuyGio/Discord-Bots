"""Randomness, and proving it was fair.

Two problems with the old implementation. First, it used ``random``, a
Mersenne Twister seeded from the clock: fine for a toy, but its entire future
output is recoverable from a few hundred observed results. Second, a player
had no way to tell whether the bot rolled honestly, which is the first thing
anyone asks when they lose a big bet.

Both are fixed the way real provably-fair casinos do it:

1. Before the round the bot generates a secret ``server_seed`` and shows the
   player ``sha256(server_seed)`` — a commitment it cannot change afterwards.
2. The outcome is derived deterministically from
   ``HMAC-SHA256(server_seed, f"{client_seed}:{nonce}")``.
3. After the round the bot reveals ``server_seed``. Anyone can hash it to
   check it matches the commitment, then re-derive the outcome themselves.

The bot therefore cannot pick a seed after seeing the bet, and cannot deny
the seed it committed to.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Sequence, TypeVar

T = TypeVar("T")

#: Cryptographically secure source, used for anything not seed-derived.
system_random = secrets.SystemRandom()


def commitment(server_seed: str) -> str:
    """The value published before a round: a hash of the secret seed."""
    return hashlib.sha256(server_seed.encode()).hexdigest()


def _digest(server_seed: str, client_seed: str, nonce: int, cursor: int = 0) -> bytes:
    message = f"{client_seed}:{nonce}:{cursor}".encode()
    return hmac.new(server_seed.encode(), message, hashlib.sha256).digest()


@dataclass(slots=True)
class Fairness:
    """One round's seed material and the stream of numbers derived from it."""

    server_seed: str = field(default_factory=lambda: secrets.token_hex(32))
    client_seed: str = ""
    nonce: int = 0
    _cursor: int = 0

    @property
    def commitment(self) -> str:
        return commitment(self.server_seed)

    @property
    def short_commitment(self) -> str:
        """First 16 hex chars — enough to display, full value on /verify."""
        return self.commitment[:16]

    def float(self) -> float:
        """Next value in ``[0, 1)`` from the seed stream."""
        digest = _digest(self.server_seed, self.client_seed, self.nonce, self._cursor)
        self._cursor += 1
        # 52 bits keeps us inside float64's exact integer range.
        value = int.from_bytes(digest[:7], "big")
        return value / float(1 << 56)

    def below(self, limit: int) -> int:
        """Next integer in ``[0, limit)``."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        return int(self.float() * limit)

    def integer(self, low: int, high: int) -> int:
        """Next integer in the inclusive range ``[low, high]``."""
        return low + self.below(high - low + 1)

    def choice(self, options: Sequence[T]) -> T:
        return options[self.below(len(options))]

    def weighted(self, options: Sequence[T], weights: Sequence[float]) -> T:
        total = float(sum(weights))
        target = self.float() * total
        running = 0.0
        for option, weight in zip(options, weights):
            running += weight
            if target < running:
                return option
        return options[-1]

    def shuffled(self, items: Sequence[T]) -> list[T]:
        """Fisher-Yates driven by the seed stream, so shuffles verify too."""
        pool = list(items)
        for index in range(len(pool) - 1, 0, -1):
            swap = self.below(index + 1)
            pool[index], pool[swap] = pool[swap], pool[index]
        return pool

    def sample(self, population: Sequence[T], count: int) -> list[T]:
        return self.shuffled(population)[:count]

    def reveal(self) -> dict[str, str]:
        """Everything a player needs to re-check the round themselves."""
        return {
            "server_seed": self.server_seed,
            "commitment": self.commitment,
            "client_seed": self.client_seed,
            "nonce": str(self.nonce),
        }


def verify(server_seed: str, published_commitment: str) -> bool:
    """Constant-time check that a revealed seed matches its commitment."""
    return hmac.compare_digest(commitment(server_seed), published_commitment.strip().lower())
