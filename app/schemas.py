"""Request / response models."""

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Nickname = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=20, pattern=r"^[\w .\-]+$")]
MatchMode = Literal["shootout", "race"]
Difficulty = Literal["rookie", "starter", "allstar", "legend"]


Hex = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$", to_lower=True)]


class Avatar(BaseModel):
    """Mirrors avatar.ts. Strict, because other players' browsers render it."""

    model_config = {"extra": "forbid"}

    version: Literal[1] = 1
    gender: Literal["male", "female"]
    skin: Hex
    hair: Literal["short", "afro", "braids", "ponytail", "bun", "bald"]
    hairColor: Hex
    jersey: Hex
    shorts: Hex
    shoes: Hex
    number: int = Field(ge=0, le=99)


class GuestCreate(BaseModel):
    nickname: Nickname
    avatar: Avatar | None = None


class UserOut(BaseModel):
    id: uuid.UUID
    nickname: str
    avatar: Avatar | None = None


class GuestOut(BaseModel):
    user: UserOut
    token: str


class ProfileUpdate(BaseModel):
    """Change your name, your look, or both."""

    nickname: Nickname | None = None
    avatar: Avatar | None = None


class RoomCreate(BaseModel):
    mode: MatchMode = "race"
    difficulty: Difficulty = "starter"
    # Race length; ignored for shootouts.
    race_seconds: int = Field(60, ge=30, le=300)


class PlayerOut(BaseModel):
    id: str
    nickname: str
    avatar: Avatar | None = None
    ready: bool
    connected: bool
    host: bool


class RoomOut(BaseModel):
    code: str
    phase: Literal["lobby", "ready", "in_progress", "finished"]
    mode: str
    difficulty: str
    max_players: int
    race_seconds: int
    host_id: str
    players: list[PlayerOut]


class ScoreCreate(BaseModel):
    # Mode key as used for bests, e.g. "free", "streak", "timed60"
    mode: Annotated[str, StringConstraints(pattern=r"^(free|streak|timed\d{2,4})$")]  # solo modes only
    difficulty: Difficulty
    score: int = Field(ge=0, le=100_000)


class LeaderboardEntry(BaseModel):
    rank: int
    user_id: uuid.UUID
    nickname: str
    best: int
    games: int
