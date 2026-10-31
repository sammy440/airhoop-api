"""
Line-for-line port of airhoops-frontend/src/lib/game/physics.ts (3D: x = toward the hoop,
y = up, z = sideways, + = the shooter's right).

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
MissKind = Literal["short", "long", "left", "right", "rim", "airball"]


@dataclass(frozen=True)
class HoopMotion:
    """The hoop slides sideways (z): offset(t) = amplitude · sin(2π · frequency · t)."""

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
    board_half_width: float = 0.915
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
    # Degrees left (−) or right (+) of straight at the hoop.
    aim: float = 0.0


@dataclass
class ShotEvent:
    t: float
    type: ShotEventType
    x: float
    y: float
    z: float
    impact: float


@dataclass
class ShotResult:
    scored: bool
    swish: bool
    bank: bool
    miss: MissKind | None
    # Depth error (+ = long) and sideways error (+ = right) where the ball first came down through rim height.
    error: float
    lateral: float


@dataclass
class ShotSim:
    launch: Launch
    path: list[tuple[float, float, float, float]]
    events: list[ShotEvent]
    result: ShotResult
    duration: float


@dataclass
class _Ball:
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float


def js_round(n: float) -> float:
    """JavaScript Math.round semantics (half rounds up), not Python's banker's rounding."""
    return math.floor(n * 1e4 + 0.5) / 1e4


def hoop_offset(court: Court, t: float) -> float:
    m = court.hoop_motion
    return m.amplitude * math.sin(2 * math.pi * m.frequency * t) if m else 0.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, v))


def _len3(x: float, y: float, z: float) -> float:
    # sqrt of a sum of squares, not hypot: IEEE sqrt rounds the same in JS and Python, hypot may not.
    return math.sqrt(x * x + y * y + z * z)


def _collide_point(b: _Ball, px: float, py: float, pz: float, r: float, restitution: float, friction: float) -> float:
    dx = b.x - px
    dy = b.y - py
    dz = b.z - pz
    dist = _len3(dx, dy, dz)
    min_dist = r + RIM_TUBE
    if dist >= min_dist or dist == 0:
        return 0.0
    nx = dx / dist
    ny = dy / dist
    nz = dz / dist
    b.x = px + nx * min_dist
    b.y = py + ny * min_dist
    b.z = pz + nz * min_dist
    vn = b.vx * nx + b.vy * ny + b.vz * nz
    if vn >= 0:
        return 0.0
    tx = b.vx - vn * nx
    ty = b.vy - vn * ny
    tz = b.vz - vn * nz
    new_vn = -vn * restitution
    b.vx = new_vn * nx + tx * friction
    b.vy = new_vn * ny + ty * friction
    b.vz = new_vn * nz + tz * friction
    return -vn


def simulate_shot(launch: Launch, court: Court = DEFAULT_COURT, wind: float = 0.0, phase: float = 0.0) -> ShotSim:
    c = court
    r = c.ball_radius
    el = launch.angle * math.pi / 180
    az = launch.aim * math.pi / 180
    flat = launch.speed * math.cos(el)
    b = _Ball(0.0, c.release_height, 0.0, flat * math.cos(az), launch.speed * math.sin(el), flat * math.sin(az))

    board_lo = c.rim_height + c.board_bottom
    board_hi = c.rim_height + c.board_top
    board_x = c.hoop_distance + c.rim_radius + c.board_gap

    path: list[tuple[float, float, float, float]] = [(0.0, b.x, b.y, b.z)]
    events: list[ShotEvent] = []
    touched_rim = touched_board = False
    scored_at: float | None = None
    descent: tuple[float, float, float] | None = None  # (x, z, hoop z)
    t = 0.0
    step = 0
    last_floor = -1.0

    while t < MAX_TIME:
        prev_y = b.y
        hoop_z = hoop_offset(c, phase + t)
        b.vz += wind * DT
        b.vy -= c.gravity * DT
        b.x += b.vx * DT
        b.y += b.vy * DT
        b.z += b.vz * DT
        step += 1
        t = step * DT

        # Rim: the nearest point of the ring.
        hx = b.x - c.hoop_distance
        hz = b.z - hoop_z
        ring_dist = _len3(hx, 0.0, hz)
        if ring_dist > 0:
            px = c.hoop_distance + (hx / ring_dist) * c.rim_radius
            pz = hoop_z + (hz / ring_dist) * c.rim_radius
            impact = _collide_point(b, px, c.rim_height, pz, r, RESTITUTION_RIM, FRICTION_RIM)
            if impact > 0.3:
                touched_rim = True
                events.append(ShotEvent(t, "rim", px, c.rim_height, pz, impact))

        # Backboard face, then its edges (bottom, top, left, right).
        z_lo = hoop_z - c.board_half_width
        z_hi = hoop_z + c.board_half_width
        if b.x + r > board_x and b.x - r < board_x + 0.05 and board_lo < b.y < board_hi and z_lo < b.z < z_hi and b.vx > 0:
            impact = b.vx
            b.x = board_x - r
            b.vx = -b.vx * RESTITUTION_BOARD
            touched_board = True
            events.append(ShotEvent(t, "board", board_x, b.y, b.z, impact))
        for py, pz in (
            (board_lo, _clamp(b.z, z_lo, z_hi)),
            (board_hi, _clamp(b.z, z_lo, z_hi)),
            (_clamp(b.y, board_lo, board_hi), z_lo),
            (_clamp(b.y, board_lo, board_hi), z_hi),
        ):
            impact = _collide_point(b, board_x, py, pz, r, RESTITUTION_RIM, FRICTION_RIM)
            if impact > 0.3:
                touched_board = True
                events.append(ShotEvent(t, "board", board_x, py, pz, impact))

        if b.y - r < 0:
            impact = -b.vy
            b.y = r
            b.vy = -b.vy * RESTITUTION_FLOOR
            b.vx *= FRICTION_FLOOR
            b.vz *= FRICTION_FLOOR
            if impact > 0.5 and t - last_floor > 0.05:
                events.append(ShotEvent(t, "floor", b.x, 0.0, b.z, impact))
                last_floor = t

        if prev_y >= c.rim_height and b.y < c.rim_height and b.vy < 0:
            if descent is None:
                descent = (b.x, b.z, hoop_z)
            if scored_at is None and _len3(b.x - c.hoop_distance, 0.0, b.z - hoop_z) < c.rim_radius - RIM_TUBE:
                scored_at = t
                events.append(ShotEvent(t, "score", b.x, c.rim_height, b.z, _len3(b.vx, b.vy, b.vz)))

        if step % SAMPLE_EVERY == 0:
            path.append((js_round(t), js_round(b.x), js_round(b.y), js_round(b.z)))

        settled = b.y <= r + 1e-3 and abs(b.vy) < 0.4
        if (scored_at is not None and t - scored_at > AFTER_SCORE) or b.x < -2 or b.x > c.hoop_distance + 4 or abs(b.z) > 6 or (settled and t > 0.5):
            break

    scored = scored_at is not None
    error = (descent[0] if descent else b.x) - c.hoop_distance
    lateral = (descent[1] if descent else b.z) - (descent[2] if descent else 0.0)
    miss: MissKind | None = None
    if not scored:
        if touched_rim:
            miss = "rim"
        elif touched_board:
            miss = "long"
        elif descent is None:
            miss = "airball" if _len3(error, 0.0, lateral) > 1 else "short"
        elif abs(lateral) > abs(error):
            miss = "airball" if abs(lateral) > 1 else "right" if lateral > 0 else "left"
        elif error < -c.rim_radius - r:
            miss = "airball" if abs(error) > 1 else "short"
        else:
            miss = "long"

    return ShotSim(
        launch=launch,
        path=path,
        events=events,
        result=ShotResult(scored, scored and not touched_rim and not touched_board, scored and touched_board, miss, error, lateral),
        duration=t,
    )
