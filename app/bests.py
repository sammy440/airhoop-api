"""
Personal bests and their ghosts, kept with the account so they follow a player to every device.

The browser keeps its bests and ghosts in localStorage and merges with these both ways
(cloudBests.ts). They're private and self-reported, like solo scores, so they get the same sanity
checks; a fake best only fools its owner.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, col, select

from .game import daily
from .models import SavedBest, utcnow
from .schemas import CLUTCH_MAKES, MAX_MAKE_POINTS, BestIn, BestOut, BestSaved

# Daily bests stop mattering once that challenge is over; keep sending only the recent ones.
DAILY_DAYS = 2


def _difficulty(key: str) -> str | None:
    if key.startswith("daily"):
        day = daily.parse_key(key)
        return daily.daily_rules(day).difficulty if day else None
    return key.split(":", 1)[1]


def _seconds(key: str) -> int | None:
    """The run's clock: timed and daily runs only."""
    if key.startswith("timed"):
        return int(key[5:].split(":", 1)[0])
    if key.startswith("daily"):
        day = daily.parse_key(key)
        return daily.daily_rules(day).seconds if day else None
    return None


def check(key: str, best: BestIn) -> str | None:
    """Why this best is impossible, or None if it's plausible."""
    difficulty = _difficulty(key)
    if difficulty is None:
        return "not a real daily challenge"
    if key.startswith("timed") and not 30 <= (_seconds(key) or 0) <= 1800:
        return "timed runs are 30 s to 30 min"
    if best.streak > best.makes:
        return "streak longer than makes"
    if best.points > (best.makes + min(best.makes, CLUTCH_MAKES)) * MAX_MAKE_POINTS[difficulty]:
        return "points too high for the makes"
    if best.ghost is not None:
        seconds = _seconds(key)
        if seconds is None:
            return "only runs on the clock have ghosts"
        if not best.ghost:
            return "empty ghost"
        if len(best.ghost) > seconds:
            return "too many shots for the clock"
        if any(s.t > seconds + 1 for s in best.ghost):
            return "shot after the buzzer"
        if sum(s.points for s in best.ghost) != best.points or sum(1 for s in best.ghost if s.points) != best.makes:
            return "ghost doesn't add up to the run"
    return None


def better(key: str, a: BestIn | SavedBest, b: SavedBest) -> bool:
    """Does `a` beat `b`? Streak boards are won on the streak, the rest on points (makes break ties)."""
    if key.startswith("streak"):
        return (a.streak, a.makes) > (b.streak, b.makes)
    return (a.points, a.makes) > (b.points, b.makes)


def out(row: SavedBest) -> BestOut:
    return BestOut(points=row.points, makes=row.makes, streak=row.streak, at=row.at, has_ghost=bool(row.ghost))


def list_bests(s: Session, user_id: uuid.UUID) -> dict[str, BestOut]:
    oldest = f"daily{(datetime.now(UTC) - timedelta(days=DAILY_DAYS)).strftime('%Y%m%d')}"
    rows = s.exec(select(SavedBest).where(SavedBest.user_id == user_id)).all()
    return {r.key: out(r) for r in rows if not r.key.startswith("daily") or r.key >= oldest}


def ghost(s: Session, user_id: uuid.UUID, key: str) -> list | None:
    row = s.get(SavedBest, (user_id, key))
    return row.ghost if row else None


def save(s: Session, user_id: uuid.UUID, key: str, best: BestIn) -> BestSaved:
    """Keeps whichever is better: this best, or the one already stored. Commits."""
    row = s.exec(select(SavedBest).where(SavedBest.user_id == user_id, col(SavedBest.key) == key).with_for_update()).first()
    if row is not None and not better(key, best, row):
        return BestSaved(**out(row).model_dump(), updated=False)
    ghost_shots = [g.model_dump() for g in best.ghost] if best.ghost else None
    if row is None:
        row = SavedBest(user_id=user_id, key=key, points=best.points, makes=best.makes, streak=best.streak, at=best.at, ghost=ghost_shots)
    else:
        row.points, row.makes, row.streak, row.at, row.ghost = best.points, best.makes, best.streak, best.at, ghost_shots
        row.updated_at = utcnow()
    s.add(row)
    s.commit()
    s.refresh(row)
    return BestSaved(**out(row).model_dump(), updated=True)
