"""
The green rim (camera play). Mirrors src/lib/game/green.ts in the frontend line for line
(the parity fixtures check greenLaunch against green_launch).

A light loops around the rim every GREEN_CYCLE_S seconds; while it passes the front, the rim is
green, and a camera throw released then swishes however it was thrown. Timed on the match
clock (the hoop-motion phase), so the server sees the same green as everyone's screen.
"""

from __future__ import annotations

import math

from .physics import DEFAULT_COURT, Court, Launch, hoop_offset, ideal_speed, simulate_shot

GREEN_CYCLE_S = 3.0

ANGLE = 52.0
# Wider than a thrown shot's ±12°: on Legend, full wind plus the hoop at the end of its slide needs ~14°.
MAX_AIM = 20.0
# Speed nudges (fraction) and aim nudges (degrees) tried, in this order, until one swishes.
SPEED_STEPS = (0, 0.005, -0.005, 0.01, -0.01, 0.015, -0.015, 0.02, -0.02, 0.03, -0.03, 0.04, -0.04)
AIM_STEPS = (0, 0.25, -0.25, 0.5, -0.5, 0.75, -0.75, 1, -1, 1.5, -1.5, 2, -2)


def green_loop(phase: float) -> float:
    """Where the light is on its loop, 0..1; 0.5 = the front of the rim."""
    return (phase % GREEN_CYCLE_S) / GREEN_CYCLE_S


def is_green(phase: float, window: float) -> bool:
    return abs(green_loop(phase) * GREEN_CYCLE_S - GREEN_CYCLE_S / 2) <= window / 2


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def green_launch(court: Court = DEFAULT_COURT, wind: float = 0.0, phase: float = 0.0) -> Launch | None:
    """A launch that swishes under these conditions, or failing that one that scores; None if neither."""
    v0 = ideal_speed(ANGLE, court)
    if v0 is None:
        return None
    def rad(d: float) -> float:
        return d * math.pi / 180  # same order as the browser's (d * Math.PI) / 180, so the same rounding

    aim = 0.0
    for _ in range(4):
        flat = v0 * math.cos(rad(ANGLE))
        vx = flat * math.cos(rad(aim))
        vz = flat * math.sin(rad(aim))
        t = court.hoop_distance / vx
        miss = vz * t + 0.5 * wind * t * t - hoop_offset(court, phase + t)
        aim = _clamp(aim - math.atan2(miss, court.hoop_distance) * 180 / math.pi, -MAX_AIM, MAX_AIM)
    make: Launch | None = None
    for ds in SPEED_STEPS:
        for da in AIM_STEPS:
            launch = Launch(speed=v0 * (1 + ds), angle=ANGLE, aim=_clamp(aim + da, -MAX_AIM, MAX_AIM))
            r = simulate_shot(launch, court, wind=wind, phase=phase).result
            if r.swish:
                return launch
            if r.scored and make is None:
                make = launch
    return make
