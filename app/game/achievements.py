"""
Achievements: moments worth marking. Mirrors ACHIEVEMENTS in the frontend's src/lib/game/achievements.ts
(tests/test_achievements.py and the frontend's achievements.test.ts pin the same ids).

Three ways to earn one:
  • totals: counted by the server from your shooting history (`player_days`: solo stats sent with /xp,
    refereed match throws), so they're as trustworthy as solo XP;
  • match: awarded at the end of a refereed multiplayer match;
  • claimed: solo moments only the browser sees (a buzzer beater, a green-rim swish, every court),
    reported with POST /me/achievements/{id}. Self-reported, like solo scores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

Stat = Literal["shots", "makes", "swishes", "banks", "best_streak", "dailies", "daily_run"]

# id → (stat, at least)
TOTALS: dict[str, tuple[Stat, int]] = {
    "first_bucket": ("makes", 1),
    "swish_10": ("swishes", 10),
    "swish_100": ("swishes", 100),
    "bank_10": ("banks", 10),
    "shots_1000": ("shots", 1000),
    "shots_10000": ("shots", 10_000),
    "streak_5": ("best_streak", 5),
    "streak_10": ("best_streak", 10),
    "streak_20": ("best_streak", 20),
    "daily_1": ("dailies", 1),
    "daily_7": ("daily_run", 7),
}
MATCH = ("first_win", "horse_clean", "shootout_perfect", "series_win")
CLAIMABLE = ("buzzer_beater", "green_light", "world_tour")
ALL = (*TOTALS, *MATCH, *CLAIMABLE)

SHOOTOUT_SHOTS = 5


@dataclass
class Totals:
    shots: int = 0
    makes: int = 0
    swishes: int = 0
    banks: int = 0
    best_streak: int = 0
    dailies: int = 0
    daily_run: int = 0


def longest_run(days: list[str]) -> int:
    """Most daily challenges in a row, from their days ("YYYYMMDD", any order)."""
    dates = sorted({datetime.strptime(d, "%Y%m%d").date() for d in days})
    best = run = 0
    prev: date | None = None
    for d in dates:
        run = run + 1 if prev is not None and d - prev == timedelta(days=1) else 1
        best = max(best, run)
        prev = d
    return best


def from_totals(t: Totals) -> set[str]:
    return {aid for aid, (stat, at_least) in TOTALS.items() if getattr(t, stat) >= at_least}


def from_match(mode: str, row: dict[str, Any], won: bool, series_won: bool) -> set[str]:
    """A player's refereed line at the end of a match (not an aborted one)."""
    if not won or row.get("forfeited"):
        return set()
    out = {"first_win"}
    if mode == "horse" and row.get("letters", 0) == 0:
        out.add("horse_clean")
    if mode == "shootout" and row.get("shots", 0) >= SHOOTOUT_SHOTS and row.get("makes") == row.get("shots"):
        out.add("shootout_perfect")
    if series_won:
        out.add("series_win")
    return out
