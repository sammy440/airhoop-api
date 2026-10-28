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
