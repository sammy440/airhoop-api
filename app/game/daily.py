"""
Daily Challenge rules: port of the frontend's daily.ts. Keep in sync.

One timed run per UTC day, the same for everyone. Its leaderboard mode key is
"daily" + the date ("daily20261030"). The difficulty and clock come from a
32-bit hash of the day number, so the server can check a posted run used
that day's rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

DIFFICULTIES = ("starter", "allstar", "allstar", "legend")
SECONDS = (30, 60, 60, 90)
EPOCH = date(1970, 1, 1)
MASK = 0xFFFFFFFF


def mix32(n: int) -> int:
    """Same avalanche mix as mix32() in daily.ts (Math.imul → multiply mod 2³²)."""
    x = n & MASK
    x = ((x ^ (x >> 16)) * 0x45D9F3B) & MASK
    x = ((x ^ (x >> 16)) * 0x45D9F3B) & MASK
    return x ^ (x >> 16)


@dataclass(frozen=True)
class DailyRules:
    key: str
    day: date
    difficulty: str
    seconds: int


def daily_rules(day: date) -> DailyRules:
    seed = mix32((day - EPOCH).days)
    return DailyRules(
        key=f"daily{day:%Y%m%d}",
        day=day,
        difficulty=DIFFICULTIES[seed % len(DIFFICULTIES)],
        seconds=SECONDS[(seed >> 8) % len(SECONDS)],
    )


def parse_key(key: str) -> date | None:
    """"daily20261030" → date(2026, 10, 30), or None if it isn't a real date."""
    try:
        return datetime.strptime(key.removeprefix("daily"), "%Y%m%d").date()
    except ValueError:
        return None


def today() -> date:
    return datetime.now(UTC).date()
