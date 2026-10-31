"""
Ports of the frontend's launch mapping (shot.ts), difficulty table
(difficulty.ts) and scoring (scoring.ts). Keep in sync.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .physics import Court, HoopMotion, Launch, ShotResult

# ── shot.ts: throw → launch ──


@dataclass(frozen=True)
class LaunchTuning:
    min_speed: float = 5.5
    max_speed: float = 11.5
    base_angle: float = 52
    angle_gain: float = 0.3
    arc_gain: float = 14
    min_angle: float = 35
    max_angle: float = 70
    aim_gain: float = 0.3
    max_aim: float = 12


TUNING = LaunchTuning()


def clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def throw_to_launch(power: float, angle: float, arc: float, aim: float = 0.0, tuning: LaunchTuning = TUNING) -> Launch:
    """
    Same mapping as throwToLaunch() in shot.ts, except power is NOT clamped to
    0..1: calibrated clients legitimately send slightly outside it (their
    speed is clamped to [0.6×min, 1.2×max]); the server validates the range.
    """
    return Launch(
        speed=tuning.min_speed + power * (tuning.max_speed - tuning.min_speed),
        angle=clamp(tuning.base_angle + (angle - 30) * tuning.angle_gain + (arc - 0.7) * tuning.arc_gain, tuning.min_angle, tuning.max_angle),
        aim=clamp(aim * tuning.aim_gain, -tuning.max_aim, tuning.max_aim),
    )


# Range a legitimate client can produce (see calibration.ts clamps); anything outside is rejected.
POWER_RANGE = ((TUNING.min_speed * 0.6 - TUNING.min_speed) / (TUNING.max_speed - TUNING.min_speed) - 1e-6,
               (TUNING.max_speed * 1.2 - TUNING.min_speed) / (TUNING.max_speed - TUNING.min_speed) + 1e-6)

# ── difficulty.ts ──


@dataclass(frozen=True)
class Difficulty:
    id: str
    distance: float
    rim_radius: float
    hoop_motion: HoopMotion | None
    wind_max: float
    score_multiplier: float


DIFFICULTIES: dict[str, Difficulty] = {
    "rookie": Difficulty("rookie", 3.6, 0.27, None, 0, 1),
    "starter": Difficulty("starter", 4.6, 0.2286, None, 0, 1),
    "allstar": Difficulty("allstar", 5.8, 0.2286, HoopMotion(0.45, 0.22), 0.6, 1.5),
    "legend": Difficulty("legend", 6.75, 0.2286, HoopMotion(0.7, 0.3), 1.2, 2),
}


def court_for(d: Difficulty) -> Court:
    return Court(hoop_distance=d.distance, rim_radius=d.rim_radius, hoop_motion=d.hoop_motion)


def roll_wind(d: Difficulty, rng: random.Random | None = None) -> float:
    if not d.wind_max:
        return 0.0
    r = (rng or random).random()
    return round((r * 2 - 1) * d.wind_max * 10) / 10


# ── scoring.ts ──

BASE_POINTS = 2
SWISH_BONUS = 1
STREAK_TIERS = ((0, 1.0), (3, 1.5), (5, 2.0), (10, 3.0))


def streak_multiplier(streak_before: int) -> float:
    m = 1.0
    for start, mult in STREAK_TIERS:
        if streak_before >= start:
            m = mult
    return m


def score_shot(result: ShotResult, streak_before: int, difficulty_multiplier: float = 1) -> tuple[int, float, int]:
    """Returns (points, multiplier, streak_after). Matches scoreShot() incl. JS Math.round."""
    if not result.scored:
        return 0, 1.0, 0
    multiplier = streak_multiplier(streak_before) * difficulty_multiplier
    raw = (BASE_POINTS + (SWISH_BONUS if result.swish else 0)) * multiplier
    points = int(raw // 1 + (1 if raw % 1 >= 0.5 else 0))  # JS Math.round (half up)
    return points, multiplier, streak_before + 1
