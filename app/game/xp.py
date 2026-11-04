"""
XP and levels. Mirrors src/lib/game/xp.ts in the frontend line for line (checked by
tests/test_xp.py against the same examples as xp.test.ts).

XP comes from what you did, never from a number the client claims: the client sends
its play stats and the server works the XP out itself.
"""

from __future__ import annotations

from dataclasses import dataclass

XP_SHOT = 1  # every shot counts a little, so a cold day still moves the bar
XP_MAKE = 10
XP_SWISH = 5  # on top of the make
XP_BANK = 5
XP_STREAK = 5  # per make in your best streak beyond the second
XP_RUN = 25  # finishing a timed, streak or daily run
XP_DAILY = 50  # first finished daily challenge of the day
XP_MATCH = 20  # finishing a multiplayer game
XP_WIN = 50

MAX_LEVEL = 50


@dataclass(frozen=True)
class XpStats:
    shots: int = 0
    makes: int = 0
    swishes: int = 0
    banks: int = 0
    best_streak: int = 0
    runs: int = 0
    dailies: int = 0
    matches: int = 0
    wins: int = 0


def xp_for(s: XpStats) -> int:
    return (
        s.shots * XP_SHOT
        + s.makes * XP_MAKE
        + s.swishes * XP_SWISH
        + s.banks * XP_BANK
        + max(0, s.best_streak - 2) * XP_STREAK
        + s.runs * XP_RUN
        + s.dailies * XP_DAILY
        + s.matches * XP_MATCH
        + s.wins * XP_WIN
    )


def xp_to_reach(level: int) -> int:
    """Total XP at which `level` starts: 100 for level 2, and each level after needs 50 more than the last."""
    n = max(1, min(level, MAX_LEVEL)) - 1
    return 100 * n + 25 * n * (n - 1)


def level_for(xp: int) -> int:
    level = 1
    while level < MAX_LEVEL and xp >= xp_to_reach(level + 1):
        level += 1
    return level
