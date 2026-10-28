import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

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
    # Linked to a Google account (the player can sign in on other devices).
    google: bool = False


class GuestOut(BaseModel):
    user: UserOut
    token: str


class GoogleSignIn(BaseModel):
    # ID token from Google Identity Services.
    credential: Annotated[str, StringConstraints(min_length=20, max_length=4096)]
    # This device's guest token, if any: a first Google sign-in links that guest (keeping its scores).
    guest_token: Annotated[str, StringConstraints(max_length=128)] | None = None
    # Used only when a brand-new player is created (no guest, no linked account yet).
    nickname: Nickname | None = None
    avatar: Avatar | None = None


class GoogleOut(BaseModel):
    user: UserOut
    token: str
    # new: a player was created · linked: this device's guest is now on Google · existing: signed in to a linked player
    account: Literal["new", "linked", "existing"]


class ProfileUpdate(BaseModel):
    nickname: Nickname | None = None
    avatar: Avatar | None = None


class RoomCreate(BaseModel):
    mode: MatchMode = "race"
    difficulty: Difficulty = "starter"
    # Race length; ignored for shootouts.
    race_seconds: int = Field(60, ge=30, le=300)
    # Series length: first to best_of // 2 + 1 wins, across rematches.
    best_of: Literal[1, 3, 5] = 1


class PlayerOut(BaseModel):
    id: str
    nickname: str
    avatar: Avatar | None = None
    ready: bool
    connected: bool
    host: bool


class SeriesOut(BaseModel):
    best_of: int
    wins: dict[str, int]
    games: int
    winner: str | None = None


class RoomOut(BaseModel):
    code: str
    phase: Literal["lobby", "ready", "in_progress", "finished"]
    mode: str
    difficulty: str
    max_players: int
    race_seconds: int
    host_id: str
    players: list[PlayerOut]
    series: SeriesOut


# Most points one make can earn: (2 + swish 1) × streak ×3 × difficulty.
MAX_MAKE_POINTS = {"rookie": 9, "starter": 9, "allstar": 13.5, "legend": 18}


class ScoreCreate(BaseModel):
    """
    A finished solo run. Solo play is simulated in the browser, so this is
    self-reported; the run stats let the server reject impossible results.
    `score` is points for timed runs and the best streak for streak runs.
    """

    # Mode key as used for bests, e.g. "streak", "timed60" (free play has no leaderboard)
    mode: Annotated[str, StringConstraints(pattern=r"^(free|streak|timed\d{2,4})$")]
    difficulty: Difficulty
    score: int = Field(ge=0, le=100_000)
    shots: int = Field(ge=0, le=10_000)
    makes: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def plausible(self) -> "ScoreCreate":
        if self.makes > self.shots:
            raise ValueError("makes cannot exceed shots")
        if self.mode == "streak":
            if self.score > self.makes:
                raise ValueError("streak longer than makes")
        elif self.score > self.makes * MAX_MAKE_POINTS[self.difficulty]:
            raise ValueError("score too high for the makes")
        if self.mode.startswith("timed"):
            seconds = int(self.mode[5:])
            if not 30 <= seconds <= 1800:
                raise ValueError("timed runs are 30 s to 30 min")
            if self.shots > seconds:  # a shot takes well over a second
                raise ValueError("too many shots for the clock")
        return self


class ScoreSaved(BaseModel):
    status: Literal["saved"] = "saved"
    best: int
    rank: int
    """All-time rank for this mode + difficulty."""


class LeaderboardEntry(BaseModel):
    rank: int
    user_id: uuid.UUID
    nickname: str
    avatar: Avatar | None = None
    best: int
    games: int


class PersonalBest(BaseModel):
    best: int
    games: int
    rank: int


class PersonalBests(BaseModel):
    """Your standing on each board period (null = no runs in that period)."""

    day: PersonalBest | None
    week: PersonalBest | None
    all: PersonalBest | None


class ClientError(BaseModel):
    """A crash report from the browser (no video, no landmarks: just the error)."""

    message: str = Field(max_length=500)
    stack: str | None = Field(default=None, max_length=4000)
    url: str | None = Field(default=None, max_length=300)
    release: str | None = Field(default=None, max_length=64)
