"""
Line-for-line port of airhoops-frontend/src/lib/game/physics.ts.

The server re-simulates every multiplayer throw with this module, so it must
produce the same shot as the browser. tests/test_physics_parity.py replays a
grid of shots exported from the TypeScript version and compares them.
Change both files together.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

ShotEventType = Literal["rim", "board", "floor", "score"]
MissKind = Literal["short", "long", "rim", "airball"]


@dataclass(frozen=True)
class HoopMotion:
    amplitude: float
    frequency: float


@dataclass(frozen=True)
class Court:
    hoop_distance: float = 4.6
    rim_height: float = 3.05
    rim_radius: float = 0.2286
    release_height: float = 2.1
    ball_radius: float = 0.12
    board_gap: float = 0.15
    board_bottom: float = -0.15
    board_top: float = 0.9
    gravity: float = 9.81
    hoop_motion: HoopMotion | None = None


DEFAULT_COURT = Court()

DT = 1 / 240
MAX_TIME = 4.5
AFTER_SCORE = 1.0
RIM_TUBE = 0.01
RESTITUTION_RIM, RESTITUTION_BOARD, RESTITUTION_FLOOR = 0.55, 0.65, 0.62
FRICTION_RIM, FRICTION_FLOOR = 0.85, 0.88
SAMPLE_EVERY = 2


@dataclass(frozen=True)
class Launch:
    speed: float
    angle: float


@dataclass
class ShotEvent:
    t: float
    type: ShotEventType
    x: float
    y: float
    impact: float


@dataclass
class ShotResult:
    scored: bool
    swish: bool
    bank: bool
    miss: MissKind | None
    error: float


@dataclass
class ShotSim:
    launch: Launch
    path: list[tuple[float, float, float]]
    events: list[ShotEvent]
    result: ShotResult
    duration: float


@dataclass
class _Ball:
    x: float
    y: float
    vx: float
    vy: float


def js_round(n: float) -> float:
    """JavaScript Math.round semantics (half rounds up), not Python's banker's rounding."""
    return math.floor(n * 1e4 + 0.5) / 1e4


def hoop_offset(court: Court, t: float) -> float:
    m = court.hoop_motion
    return m.amplitude * math.sin(2 * math.pi * m.frequency * t) if m else 0.0


def _collide_point(b: _Ball, px: float, py: float, r: float) -> float:
    dx = b.x - px
    dy = b.y - py
    dist = math.hypot(dx, dy)
    min_dist = r + RIM_TUBE
    if dist >= min_dist or dist == 0:
        return 0.0
    nx = dx / dist
    ny = dy / dist
    b.x = px + nx * min_dist
    b.y = py + ny * min_dist
    vn = b.vx * nx + b.vy * ny
    if vn >= 0:
        return 0.0
    tx = -ny
    ty = nx
    vt = b.vx * tx + b.vy * ty
    new_vn = -vn * RESTITUTION_RIM
    new_vt = vt * FRICTION_RIM
    b.vx = new_vn * nx + new_vt * tx
    b.vy = new_vn * ny + new_vt * ty
    return -vn


def simulate_shot(launch: Launch, court: Court = DEFAULT_COURT, wind: float = 0.0, phase: float = 0.0) -> ShotSim:
    c = court
    r = c.ball_radius
    rad = launch.angle * math.pi / 180
    b = _Ball(0.0, c.release_height, launch.speed * math.cos(rad), launch.speed * math.sin(rad))

    board_lo = c.rim_height + c.board_bottom
    board_hi = c.rim_height + c.board_top

    path: list[tuple[float, float, float]] = [(0.0, b.x, b.y)]
    events: list[ShotEvent] = []
    touched_rim = touched_board = False
    scored_at: float | None = None
    first_descent_x: float | None = None
    descent_hoop_x = c.hoop_distance
    t = 0.0
    step = 0
    last_floor = -1.0

    while t < MAX_TIME:
        prev_y = b.y
        hoop_x = c.hoop_distance + hoop_offset(c, phase + t)
        rim_front = hoop_x - c.rim_radius
        rim_back = hoop_x + c.rim_radius
        board_x = rim_back + c.board_gap
        b.vx += wind * DT
        b.vy -= c.gravity * DT
        b.x += b.vx * DT
        b.y += b.vy * DT
        step += 1
        t = step * DT

        for px in (rim_front, rim_back):
            impact = _collide_point(b, px, c.rim_height, r)
            if impact > 0.3:
                touched_rim = True
                events.append(ShotEvent(t, "rim", px, c.rim_height, impact))

        if b.x + r > board_x and b.x - r < board_x + 0.05 and board_lo < b.y < board_hi and b.vx > 0:
            impact = b.vx
            b.x = board_x - r
            b.vx = -b.vx * RESTITUTION_BOARD
            touched_board = True
            events.append(ShotEvent(t, "board", board_x, b.y, impact))
        for py in (board_lo, board_hi):
            impact = _collide_point(b, board_x, py, r)
            if impact > 0.3:
                touched_board = True
                events.append(ShotEvent(t, "board", board_x, py, impact))

        if b.y - r < 0:
            impact = -b.vy
            b.y = r
            b.vy = -b.vy * RESTITUTION_FLOOR
            b.vx *= FRICTION_FLOOR
            if impact > 0.5 and t - last_floor > 0.05:
                events.append(ShotEvent(t, "floor", b.x, 0.0, impact))
                last_floor = t

        if prev_y >= c.rim_height and b.y < c.rim_height and b.vy < 0:
            if first_descent_x is None:
                first_descent_x = b.x
                descent_hoop_x = hoop_x
            if scored_at is None and abs(b.x - hoop_x) < c.rim_radius - RIM_TUBE:
                scored_at = t
                events.append(ShotEvent(t, "score", b.x, c.rim_height, math.hypot(b.vx, b.vy)))

        if step % SAMPLE_EVERY == 0:
            path.append((js_round(t), js_round(b.x), js_round(b.y)))

        settled = b.y <= r + 1e-3 and abs(b.vy) < 0.4
        if (scored_at is not None and t - scored_at > AFTER_SCORE) or b.x < -2 or b.x > c.hoop_distance + 4 or (settled and t > 0.5):
            break

    scored = scored_at is not None
    error = (first_descent_x if first_descent_x is not None else b.x) - descent_hoop_x
    miss: MissKind | None = None
    if not scored:
        if touched_rim:
            miss = "rim"
        elif touched_board:
            miss = "long"
        elif first_descent_x is None or error < -c.rim_radius - r:
            miss = "airball" if abs(error) > 1 else "short"
        else:
            miss = "long"

    return ShotSim(
        launch=launch,
        path=path,
        events=events,
        result=ShotResult(scored, scored and not touched_rim and not touched_board, scored and touched_board, miss, error),
        duration=t,
    )

