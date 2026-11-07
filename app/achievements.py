"""Achievements in the database: who earned what, and checking for new ones (rules: app/game/achievements.py)."""

import uuid
from collections.abc import Iterable

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, col, select

from .game.achievements import Totals, from_totals, longest_run
from .models import PlayerDay, UserAchievement


def award(s: Session, user_id: uuid.UUID, ids: Iterable[str]) -> list[str]:
    """Records achievements; returns the ones that are new. Doesn't commit."""
    ids = sorted(set(ids))
    if not ids:
        return []
    new = s.exec(
        insert(UserAchievement)
        .values([{"user_id": user_id, "achievement": a} for a in ids])
        .on_conflict_do_nothing()
        .returning(UserAchievement.achievement)
    ).all()
    return sorted(r[0] for r in new)


def totals(s: Session, user_id: uuid.UUID) -> Totals:
    row = s.exec(
        select(
            func.coalesce(func.sum(PlayerDay.shots), 0),
            func.coalesce(func.sum(PlayerDay.makes), 0),
            func.coalesce(func.sum(PlayerDay.swishes), 0),
            func.coalesce(func.sum(PlayerDay.banks), 0),
            func.coalesce(func.max(PlayerDay.best_streak), 0),
        ).where(PlayerDay.user_id == user_id)
    ).one()
    dailies = s.exec(select(PlayerDay.day).where(PlayerDay.user_id == user_id, col(PlayerDay.daily))).all()
    return Totals(*(int(v) for v in row), dailies=len(set(dailies)), daily_run=longest_run(list(dailies)))


def check_totals(s: Session, user_id: uuid.UUID) -> list[str]:
    """Awards whatever the player's shooting history now earns. Doesn't commit."""
    s.flush()
    return award(s, user_id, from_totals(totals(s, user_id)))


def earned(s: Session, user_id: uuid.UUID) -> list[UserAchievement]:
    return list(s.exec(select(UserAchievement).where(UserAchievement.user_id == user_id).order_by(col(UserAchievement.unlocked_at))).all())
