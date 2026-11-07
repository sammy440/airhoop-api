"""
Match history and shooting trends: what the profile shows about a player's own play.

Writes: `add_day` adds shots to a player's day (solo stats from /xp, refereed match throws from the
room hooks). Reads: `match_history` and `shooting_stats`, behind `GET /me/matches` and `GET /me/stats`.
"""

import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from .models import Match, MatchPlayer, PlayerDay, Throw, User
from .schemas import DayStats, HistoryMatch, HistoryPlayer, MissBias, ModeRecord, ShootingStats, ShotTotals

Source = Literal["solo", "match"]
MISS_KINDS = ("short", "long", "left", "right", "rim", "airball")


def day_key(at: datetime | None = None) -> str:
    return (at or datetime.now(UTC)).strftime("%Y%m%d")


def add_day(
    s: Session,
    user_id: uuid.UUID,
    source: Source,
    *,
    shots: int = 0,
    makes: int = 0,
    swishes: int = 0,
    banks: int = 0,
    misses: dict[str, int] | None = None,
    best_streak: int = 0,
    daily: bool = False,
    day: str | None = None,
) -> None:
    """Adds to the player's row for the day (created if needed). Doesn't commit."""
    day = day or day_key()
    # Make sure the row exists, then lock it: concurrent adds (two tabs, a sync during a match) both count.
    s.exec(insert(PlayerDay).values(user_id=user_id, day=day, source=source, shots=0, makes=0, swishes=0, banks=0).on_conflict_do_nothing())
    row = s.exec(select(PlayerDay).where(PlayerDay.user_id == user_id, PlayerDay.day == day, PlayerDay.source == source).with_for_update()).one()
    row.shots += shots
    row.makes += makes
    row.swishes += swishes
    row.banks += banks
    row.best_streak = max(row.best_streak, best_streak)
    row.daily = row.daily or daily
    if misses:
        merged = Counter(row.misses or {})
        merged.update({k: v for k, v in misses.items() if k in MISS_KINDS and v > 0})
        row.misses = dict(merged)
    s.add(row)


# ── Reads ──


def match_history(s: Session, user_id: uuid.UUID, limit: int, before: datetime | None) -> list[HistoryMatch]:
    """The player's finished matches, newest first, with everyone's line."""
    q = (
        select(Match)
        .join(MatchPlayer, col(MatchPlayer.match_id) == col(Match.id))
        .where(MatchPlayer.user_id == user_id, col(Match.ended_at).is_not(None))
        .order_by(col(Match.started_at).desc())
        .limit(limit)
    )
    if before is not None:
        q = q.where(col(Match.started_at) < before)
    matches = s.exec(q).all()
    if not matches:
        return []

    rows = s.exec(
        select(MatchPlayer, User.nickname, User.avatar)
        .join(User, col(User.id) == col(MatchPlayer.user_id))
        .where(col(MatchPlayer.match_id).in_([m.id for m in matches]))
        .order_by(col(MatchPlayer.rank), col(User.nickname))
    ).all()
    players: dict[uuid.UUID, list[HistoryPlayer]] = {}
    for mp, nickname, avatar in rows:
        players.setdefault(mp.match_id, []).append(
            HistoryPlayer(
                user_id=mp.user_id,
                nickname=nickname,
                avatar=avatar,
                rank=mp.rank,
                won=mp.won,
                forfeited=mp.forfeited,
                points=mp.points,
                shots=mp.shots,
                makes=mp.makes,
                swishes=mp.swishes,
                banks=mp.banks,
                best_streak=mp.best_streak,
                letters=mp.letters,
            )
        )

    out = []
    for m in matches:
        everyone = players.get(m.id, [])
        you = next(p for p in everyone if p.user_id == user_id)
        out.append(
            HistoryMatch(
                id=m.id,
                mode=m.mode,
                difficulty=m.difficulty,
                race_seconds=m.race_seconds,
                started_at=m.started_at,
                ended_at=m.ended_at,
                reason=m.reason,
                you=you,
                players=everyone,
            )
        )
    return out


def _totals(rows: list[PlayerDay]) -> ShotTotals:
    misses: Counter[str] = Counter()
    for r in rows:
        misses.update(r.misses or {})
    return ShotTotals(
        shots=sum(r.shots for r in rows),
        makes=sum(r.makes for r in rows),
        swishes=sum(r.swishes for r in rows),
        banks=sum(r.banks for r in rows),
        misses={k: misses[k] for k in MISS_KINDS if misses[k]},
    )


def shooting_stats(s: Session, user_id: uuid.UUID, days: int) -> ShootingStats:
    now = datetime.now(UTC)
    since = now - timedelta(days=days - 1)
    rows = s.exec(
        select(PlayerDay).where(PlayerDay.user_id == user_id, PlayerDay.day >= day_key(since)).order_by(col(PlayerDay.day))
    ).all()

    by_day: dict[str, DayStats] = {}
    for r in rows:
        d = by_day.setdefault(r.day, DayStats(day=f"{r.day[:4]}-{r.day[4:6]}-{r.day[6:]}"))
        setattr(d, r.source, _totals([r]))

    # Where refereed misses come down, on average: a habit of shooting long, or to one side.
    bias = s.exec(
        select(func.count(), func.avg(Throw.depth_error), func.avg(Throw.side_error)).where(
            Throw.user_id == user_id,
            Throw.scored == False,  # noqa: E712 - SQL comparison
            col(Throw.miss).is_not(None),
            col(Throw.miss) != "airball",  # nowhere near: no direction to learn from
            Throw.created_at >= since.replace(hour=0, minute=0, second=0, microsecond=0),
        )
    ).one()

    record = s.exec(
        select(Match.mode, func.count(), func.count().filter(col(MatchPlayer.won)))
        .join(MatchPlayer, col(MatchPlayer.match_id) == col(Match.id))
        .where(MatchPlayer.user_id == user_id, col(Match.reason) != "aborted")
        .group_by(Match.mode)
    ).all()

    return ShootingStats(
        days=days,
        totals=_totals(list(rows)),
        solo=_totals([r for r in rows if r.source == "solo"]),
        match=_totals([r for r in rows if r.source == "match"]),
        by_day=list(by_day.values()),
        miss_bias=MissBias(misses=bias[0], depth=bias[1], side=bias[2]) if bias[0] else None,
        record={mode: ModeRecord(played=played, wins=wins) for mode, played, wins in record},
    )
