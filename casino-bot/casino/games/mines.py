"""Mines: a hidden-bomb grid, and a cash-out decision on every click.

The old ``!mines`` command did not have a grid at all. It rolled
``random.randint(0, 15 - mines)``, compared it to the mine count, and paid a
flat 2x — so the "number of mines" you chose changed your odds but never your
payout, and there was nothing to click.

This is the real game. Each safe tile you reveal multiplies your stake by the
fair amount for the risk you just took, and you can bank it at any point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import comb

from casino.config import DEFAULT_RTP
from casino.games.rng import Fairness

#: Discord allows 25 components on a message and the grid needs one row for
#: the cash-out control, so the board is 5 wide by 4 tall.
WIDTH = 5
HEIGHT = 4
TILES = WIDTH * HEIGHT
MIN_MINES = 1
MAX_MINES = TILES - 1
DEFAULT_MINES = 3


def survival_chance(mines: int, picks: int) -> float:
    """Probability of revealing ``picks`` tiles without hitting a mine.

    There are ``C(TILES, picks)`` equally likely ways to choose that many
    tiles and ``C(TILES - mines, picks)`` of them avoid every mine.

    >>> round(survival_chance(1, 1), 4)
    0.95
    >>> round(survival_chance(19, 1), 4)
    0.05
    """
    if picks <= 0:
        return 1.0
    safe = TILES - mines
    if picks > safe:
        return 0.0
    return comb(safe, picks) / comb(TILES, picks)


def multiplier(mines: int, picks: int, rtp: float = DEFAULT_RTP) -> float:
    """Total return per chip staked after ``picks`` successful reveals."""
    if picks <= 0:
        return 1.0
    chance = survival_chance(mines, picks)
    if chance <= 0:
        raise ValueError("no such run is possible")
    return rtp / chance


def next_multiplier(mines: int, picks: int, rtp: float = DEFAULT_RTP) -> float:
    """What the next successful click would be worth — shown before clicking."""
    return multiplier(mines, picks + 1, rtp)


def multiplier_ladder(mines: int, rtp: float = DEFAULT_RTP, steps: int = 5) -> list[tuple[int, float]]:
    """First few rungs of the payout ladder, for the pre-game embed."""
    safe = TILES - mines
    return [(picks, multiplier(mines, picks, rtp)) for picks in range(1, min(steps, safe) + 1)]


@dataclass(slots=True)
class Board:
    """One mines round. Mine positions come from the seeded stream."""

    mines: int = DEFAULT_MINES
    rtp: float = DEFAULT_RTP
    mine_positions: frozenset[int] = field(default_factory=frozenset)
    revealed: list[int] = field(default_factory=list)
    exploded_at: int | None = None
    cashed_out: bool = False

    @classmethod
    def create(cls, mines: int, fairness: Fairness, rtp: float = DEFAULT_RTP) -> "Board":
        mines = max(MIN_MINES, min(MAX_MINES, mines))
        positions = fairness.sample(range(TILES), mines)
        return cls(mines=mines, rtp=rtp, mine_positions=frozenset(positions))

    @property
    def picks(self) -> int:
        return len(self.revealed)

    @property
    def safe_tiles(self) -> int:
        return TILES - self.mines

    @property
    def finished(self) -> bool:
        return self.cashed_out or self.exploded_at is not None or self.picks >= self.safe_tiles

    @property
    def current_multiplier(self) -> float:
        return multiplier(self.mines, self.picks, self.rtp)

    @property
    def next_multiplier(self) -> float:
        if self.picks >= self.safe_tiles:
            return self.current_multiplier
        return next_multiplier(self.mines, self.picks, self.rtp)

    @property
    def cleared(self) -> bool:
        return self.picks >= self.safe_tiles

    def is_mine(self, tile: int) -> bool:
        return tile in self.mine_positions

    def reveal(self, tile: int) -> bool:
        """Click a tile. Returns ``True`` if it was safe.

        Raises if the round is already over or the tile was already clicked,
        which keeps a double-click from advancing the multiplier twice.
        """
        if self.finished:
            raise RuntimeError("this round is already finished")
        if tile in self.revealed:
            raise ValueError("tile already revealed")
        if not 0 <= tile < TILES:
            raise ValueError("tile out of range")
        if self.is_mine(tile):
            self.exploded_at = tile
            return False
        self.revealed.append(tile)
        return True

    def cash_out(self) -> int:
        """Stop and bank the current multiplier. Returns the multiplier x100."""
        if self.finished:
            raise RuntimeError("this round is already finished")
        self.cashed_out = True
        return int(self.current_multiplier * 100)

    def payout(self, stake: int) -> int:
        """Total chips returned, stake included. Zero if a mine was hit."""
        if self.exploded_at is not None:
            return 0
        if not self.cashed_out and not self.cleared:
            return 0
        return int(stake * self.current_multiplier)
