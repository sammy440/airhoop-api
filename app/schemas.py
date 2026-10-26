"""Request / response models."""

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Nickname = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=20, pattern=r"^[\w .\-]+$")]
Mode = Literal["free", "timed", "streak"]
Difficulty = Literal["rookie", "starter", "allstar", "legend"]


class GuestCreate(BaseModel):
    nickname: Nickname


class UserOut(BaseModel):
    id: uuid.UUID
    nickname: str


class GuestOut(BaseModel):
    user: UserOut
    token: str


class Rename(BaseModel):
    nickname: Nickname


class RoomCreate(BaseModel):
    mode: Mode = "timed"
    difficulty: Difficulty = "starter"


class PlayerOut(BaseModel):
    id: str
    nickname: str
    ready: bool
    connected: bool
    host: bool


class RoomOut(BaseModel):
    code: str
    phase: Literal["lobby", "ready", "in_progress", "finished"]
    mode: str
    difficulty: str
    max_players: int
    host_id: str
    players: list[PlayerOut]


class ScoreCreate(BaseModel):
    # Mode key as used for bests, e.g. "free", "streak", "timed60"
    mode: Annotated[str, StringConstraints(pattern=r"^(free|streak|timed\d{2,4})$")]
    difficulty: Difficulty
    score: int = Field(ge=0, le=100_000)


class LeaderboardEntry(BaseModel):
    rank: int
    user_id: uuid.UUID
    nickname: str
    best: int
    games: int
