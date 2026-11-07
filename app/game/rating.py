"""
Skill rating: Elo, from refereed multiplayer results. Quick match uses it to pair strangers of similar
skill (a window that widens the longer either player waits, so nobody waits forever).

A match of 3–4 players counts as every pair in it: you beat everyone you finished above, drew with
anyone level, lost to the rest, and your change is the average over those pairs. Forfeits count as
losses (they're ranked last). Aborted matches don't count.
"""

from __future__ import annotations

DEFAULT = 1000
FLOOR = 100
# The first games move a rating faster, so a new player finds their level quickly.
PROVISIONAL_GAMES = 20
K_NEW = 40
K = 24

# Quick match: ratings this close pair at once; the gap allowed widens with the wait, and after
# OPEN_AFTER_S anyone compatible will do.
WINDOW_BASE = 150
WINDOW_PER_S = 40
OPEN_AFTER_S = 20


def expected(a: float, b: float) -> float:
    return 1 / (1 + 10 ** ((b - a) / 400))


def k_factor(games: int) -> int:
    return K_NEW if games < PROVISIONAL_GAMES else K


def changes(players: list[tuple[str, int, int, int]]) -> dict[str, int]:
    """
    `players`: (id, rating, rated games so far, rank; 1 = best, ties share a rank) → each one's change.
    """
    out: dict[str, int] = {}
    if len(players) < 2:
        return {pid: 0 for pid, *_ in players}
    for pid, rating, games, rank in players:
        total = 0.0
        for qid, other, _, other_rank in players:
            if qid == pid:
                continue
            score = 1.0 if rank < other_rank else 0.5 if rank == other_rank else 0.0
            total += score - expected(rating, other)
        delta = round(k_factor(games) * total / (len(players) - 1))
        out[pid] = max(FLOOR, rating + delta) - rating
    return out


def window(waited_s: float) -> float:
    """How far apart two quick-match ratings may be, given the longer of the two waits."""
    return float("inf") if waited_s >= OPEN_AFTER_S else WINDOW_BASE + WINDOW_PER_S * waited_s
