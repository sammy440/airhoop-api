import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.game import daily
from app.match import valid_race_seconds

Nickname = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=20, pattern=r"^[\w .\-]+$")]
MatchMode = Literal["shootout", "race", "horse"]
Difficulty = Literal["rookie", "starter", "allstar", "legend"]
MissKind = Literal["short", "long", "left", "right", "rim", "airball"]


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
    xp: int = 0
    level: int = 1
    loadout: dict[str, str] | None = None
    rating: int = 1000
    # Fewer than 20 rated matches: the rating is still settling
    provisional: bool = True


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


class Loadout(BaseModel):
    """Ball skin and trail ids (unknown or locked ones are shown as the default)."""

    ball: Annotated[str, StringConstraints(max_length=16)] = "classic"
    trail: Annotated[str, StringConstraints(max_length=16)] = "flame"


class ProfileUpdate(BaseModel):
    nickname: Nickname | None = None
    avatar: Avatar | None = None
    loadout: Loadout | None = None


class RoomCreate(BaseModel):
    mode: MatchMode = "race"
    difficulty: Difficulty = "starter"
    # Race length (30 s, 90 s or whole minutes up to 30); ignored for shootouts.
    race_seconds: int = 60
    # Series length: first to best_of // 2 + 1 wins, across rematches.
    best_of: Literal[1, 3, 5] = 1

    @field_validator("race_seconds")
    @classmethod
    def race_length(cls, v: int) -> int:
        if not valid_race_seconds(v):
            raise ValueError("race length must be 30 s, 90 s or whole minutes up to 30")
        return v


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
    # Made by quick match (strangers, auto-started)
    quick: bool = False
    # People watching (ws ?watch=1)
    spectators: int = 0


class QuickMatchRequest(BaseModel):
    difficulty: Difficulty | Literal["any"] = "any"


class QuickMatchOut(BaseModel):
    status: Literal["waiting", "matched"]
    # Matched: the room to join
    code: str | None = None
    # Waiting: players in the queue (you included) and how long you've waited
    waiting: int | None = None
    waited_s: float | None = None


# Most points one make can earn: (2 + swish or bank 1) × streak ×3 × difficulty.
MAX_MAKE_POINTS = {"rookie": 9, "starter": 9, "allstar": 13.5, "legend": 18}
# Clutch time (frontend lib/game/clutch.ts): makes in a timed run's last 10 s score double.
# A shot takes over a second, so at most this many makes earn it.
CLUTCH_MAKES = 10


class ScoreCreate(BaseModel):
    """
    A finished solo run. Solo play is simulated in the browser, so this is
    self-reported; the run stats let the server reject impossible results.
    `score` is points for timed and daily runs and the best streak for streak runs.
    """

    # Mode key as used for bests, e.g. "streak", "timed60", "daily20261030" (free play has no leaderboard)
    mode: Annotated[str, StringConstraints(pattern=r"^(free|streak|timed\d{2,4}|daily\d{8})$")]
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
        elif self.score > (self.makes + min(self.makes, CLUTCH_MAKES)) * MAX_MAKE_POINTS[self.difficulty]:
            raise ValueError("score too high for the makes")
        if self.mode.startswith("timed"):
            seconds = int(self.mode[5:])
            if not 30 <= seconds <= 1800:
                raise ValueError("timed runs are 30 s to 30 min")
            if self.shots > seconds:  # a shot takes well over a second
                raise ValueError("too many shots for the clock")
        if self.mode.startswith("daily"):
            day = daily.parse_key(self.mode)
            # ±1 day: a run that started before midnight, or a device clock a little off.
            if day is None or abs(day - daily.today()) > timedelta(days=1):
                raise ValueError("not a current daily challenge")
            rules = daily.daily_rules(day)
            if self.difficulty != rules.difficulty:
                raise ValueError("wrong difficulty for that day's challenge")
            if self.shots > rules.seconds:
                raise ValueError("too many shots for the clock")
        return self


class XpGain(BaseModel):
    """
    Solo play since the last sync: what happened, not how much XP it's worth.
    The server works the XP out (app/game/xp.py) and caps it per day.
    """

    shots: int = Field(0, ge=0, le=2000)
    makes: int = Field(0, ge=0, le=2000)
    swishes: int = Field(0, ge=0, le=2000)
    banks: int = Field(0, ge=0, le=2000)
    best_streak: int = Field(0, ge=0, le=2000)
    runs: int = Field(0, ge=0, le=50)
    # The daily challenge finished in this batch, e.g. "daily20261104" (earns its bonus once)
    daily: Annotated[str, StringConstraints(pattern=r"^daily\d{8}$")] | None = None
    # Misses by kind, for the shooting trends on the profile (no XP)
    misses: dict[MissKind, Annotated[int, Field(ge=0, le=2000)]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def plausible(self) -> "XpGain":
        if self.makes > self.shots:
            raise ValueError("makes cannot exceed shots")
        if self.swishes + self.banks > self.makes:
            raise ValueError("swishes and banks are makes")
        if self.best_streak > self.makes:
            raise ValueError("streak longer than makes")
        if sum(self.misses.values()) > self.shots - self.makes:
            raise ValueError("more misses than missed shots")
        return self


class XpOut(BaseModel):
    xp: int
    level: int
    gained: int
    # Hit today's solo limit: some of this batch didn't count
    capped: bool = False
    # Achievements this batch unlocked (ids; the browser has the names)
    achievements: list[str] = Field(default_factory=list)


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


# ── Match history and shooting trends (GET /me/matches, /me/stats) ──


class HistoryPlayer(BaseModel):
    user_id: uuid.UUID
    nickname: str
    avatar: Avatar | None = None
    rank: int
    won: bool
    forfeited: bool
    points: int
    shots: int
    makes: int
    swishes: int
    banks: int
    best_streak: int
    letters: int | None = None
    rating_change: int | None = None


class HistoryMatch(BaseModel):
    id: uuid.UUID
    mode: MatchMode
    difficulty: Difficulty
    race_seconds: int | None
    started_at: datetime
    ended_at: datetime | None
    # "complete" | "time" | "forfeit" | "aborted" (null for matches played before this was recorded)
    reason: str | None
    you: HistoryPlayer
    # Everyone, you included, best first
    players: list[HistoryPlayer]


class ShotTotals(BaseModel):
    shots: int = 0
    makes: int = 0
    swishes: int = 0
    banks: int = 0
    misses: dict[MissKind, int] = Field(default_factory=dict)


class DayStats(BaseModel):
    day: str  # YYYY-MM-DD (UTC)
    solo: ShotTotals | None = None
    match: ShotTotals | None = None


class MissBias(BaseModel):
    """Where refereed misses came down through rim height, on average (m from the rim's centre)."""

    misses: int
    depth: float  # + = long
    side: float  # + = right


class ModeRecord(BaseModel):
    played: int
    wins: int


class ShootingStats(BaseModel):
    days: int
    totals: ShotTotals
    solo: ShotTotals
    match: ShotTotals
    # Only days with play, oldest first
    by_day: list[DayStats]
    miss_bias: MissBias | None
    # All-time multiplayer record by mode (aborted games left out)
    record: dict[str, ModeRecord]


# ── Personal bests and ghosts, kept with the account (/me/bests) ──

# The browser's best keys (modes.ts bestKey): timed and daily runs, streaks, and free play per difficulty.
BEST_KEY_PATTERN = r"^((timed\d{2,4}|streak|free):(rookie|starter|allstar|legend)|daily\d{8})$"
GHOST_MAX_SHOTS = 600


class GhostShot(BaseModel):
    """One launch of a ghost run (ghostRun.ts GhostShot)."""

    t: float = Field(ge=0, le=1900)  # run time at release (s)
    speed: float = Field(ge=0, le=100)
    angle: float = Field(ge=-90, le=90)
    aim: float = Field(ge=-90, le=90)
    wind: float = Field(ge=-50, le=50)
    phase: float = Field(ge=-1000, le=100_000)
    points: int = Field(ge=0, le=200)


class BestIn(BaseModel):
    points: int = Field(ge=0, le=100_000)
    makes: int = Field(ge=0, le=10_000)
    streak: int = Field(ge=0, le=10_000)
    at: datetime
    ghost: list[GhostShot] | None = Field(default=None, max_length=GHOST_MAX_SHOTS)


class BestOut(BaseModel):
    points: int
    makes: int
    streak: int
    at: datetime
    has_ghost: bool


class BestSaved(BestOut):
    # False: the stored best was better, so it was kept (and is what's returned)
    updated: bool


class AchievementOut(BaseModel):
    id: str
    unlocked_at: datetime


class AchievementClaimed(AchievementOut):
    new: bool


# ── Invites (/invites) ──


class InviteCreate(BaseModel):
    to: uuid.UUID
    code: Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=4, max_length=8)]


class InviteOut(BaseModel):
    from_id: uuid.UUID
    nickname: str
    avatar: Avatar | None = None
    code: str
    mode: str
    difficulty: str
    race_seconds: int
    best_of: int
    players: int
    age_s: float
