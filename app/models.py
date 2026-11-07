"""Database tables."""

import uuid
from datetime import UTC, datetime

from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    nickname: str = Field(max_length=20)
    # SHA-256 of the guest token; the token itself is never stored.
    token_hash: str = Field(max_length=64, unique=True, index=True)
    # Player look (gender, skin, hair, kit colours, number); validated by schemas.Avatar.
    avatar: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    # Google account ID ("sub") once linked. Only the ID is kept: no email, name or photo.
    google_sub: str | None = Field(default=None, max_length=255, unique=True, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    # Progress (app/game/xp.py). Solo XP is self-reported play stats, so it's capped per UTC day.
    xp: int = Field(default=0)
    xp_day: str | None = Field(default=None, max_length=8)  # YYYYMMDD the counter below belongs to
    xp_today: int = Field(default=0)
    last_daily: str | None = Field(default=None, max_length=16)  # last daily challenge that earned its bonus
    # Chosen ball skin and trail ({"ball", "trail"}); shown to others only if unlocked (app/game/cosmetics.py).
    loadout: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    # Skill rating (app/game/rating.py) and how many rated matches it's from
    rating: int = Field(default=1000)
    rated_games: int = Field(default=0)


class AuthToken(SQLModel, table=True):
    """Extra sign-in tokens (one per device signed in with Google). Only the hash is stored."""

    __tablename__ = "auth_tokens"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    token_hash: str = Field(max_length=64, unique=True, index=True)
    created_at: datetime = Field(default_factory=utcnow)


class Match(SQLModel, table=True):
    __tablename__ = "matches"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    room_code: str = Field(max_length=8, index=True)
    mode: str = Field(max_length=16)
    difficulty: str = Field(max_length=16)
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    winner_id: uuid.UUID | None = Field(default=None, foreign_key="users.id")
    race_seconds: int | None = None
    # How it ended: "complete" | "time" | "forfeit" | "aborted" (null while playing, or if the server stopped)
    reason: str | None = Field(default=None, max_length=16)


class Throw(SQLModel, table=True):
    __tablename__ = "throws"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    match_id: uuid.UUID = Field(foreign_key="matches.id", index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    power: float
    angle: float
    arc: float
    scored: bool
    points: int
    created_at: datetime = Field(default_factory=utcnow)
    # From the server's re-simulation (null on throws stored before these columns existed).
    aim: float | None = None
    swish: bool | None = None
    bank: bool | None = None
    miss: str | None = Field(default=None, max_length=8)  # short | long | left | right | rim | airball
    # Where the ball came down through rim height, from the rim's centre (m): + = long / + = right
    depth_error: float | None = None
    side_error: float | None = None
    green: bool | None = None


class MatchPlayer(SQLModel, table=True):
    """Each player's result in a finished match: what match history is read from."""

    __tablename__ = "match_players"

    match_id: uuid.UUID = Field(foreign_key="matches.id", primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", primary_key=True, index=True)
    rank: int
    won: bool
    forfeited: bool = False
    points: int = 0
    shots: int = 0
    makes: int = 0
    swishes: int = 0
    banks: int = 0
    best_streak: int = 0
    letters: int | None = None  # H-O-R-S-E only
    rating_change: int | None = None  # null: unrated (aborted, or before ratings)


class PlayerDay(SQLModel, table=True):
    """
    A player's shooting on one UTC day, solo and multiplayer kept apart: the trends on the profile.
    Solo rows add up the stats sent with /xp (self-reported, like solo XP); match rows the refereed throws.
    """

    __tablename__ = "player_days"

    user_id: uuid.UUID = Field(foreign_key="users.id", primary_key=True)
    day: str = Field(max_length=8, primary_key=True)  # YYYYMMDD
    source: str = Field(max_length=8, primary_key=True)  # solo | match
    shots: int = 0
    makes: int = 0
    swishes: int = 0
    banks: int = 0
    # Misses by kind, e.g. {"short": 4, "left": 1}
    misses: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    # Longest run of makes that day
    best_streak: int = 0
    # Finished that day's daily challenge (solo rows)
    daily: bool = False


class Score(SQLModel, table=True):
    __tablename__ = "scores"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    # e.g. "free", "streak", "timed60" (timed scores only compare at equal length)
    mode: str = Field(max_length=16, index=True)
    difficulty: str = Field(max_length=16)
    score: int
    # Run stats used to sanity-check self-reported solo scores (null for multiplayer rows).
    shots: int | None = None
    makes: int | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)


class SavedBest(SQLModel, table=True):
    """
    A player's personal best for one board (the browser's best key, e.g. "timed60:rookie",
    "streak:legend", "daily20261106") and, for runs on the clock, its ghost: the launches,
    re-flown by the browser's deterministic physics. Private: only its owner reads it.
    """

    __tablename__ = "personal_bests"

    user_id: uuid.UUID = Field(foreign_key="users.id", primary_key=True)
    key: str = Field(max_length=24, primary_key=True)
    points: int
    makes: int
    streak: int
    at: datetime
    ghost: list[Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    updated_at: datetime = Field(default_factory=utcnow)


class UserAchievement(SQLModel, table=True):
    """An achievement a player has earned (app/game/achievements.py)."""

    __tablename__ = "user_achievements"

    user_id: uuid.UUID = Field(foreign_key="users.id", primary_key=True)
    achievement: str = Field(max_length=24, primary_key=True)
    unlocked_at: datetime = Field(default_factory=utcnow)
