"""
Server-authoritative match engine.

Pure game logic: every method takes `now` (server seconds) and returns the
messages to send, so the rules are unit-testable without sockets. The room
manager owns delivery and persistence.

Modes
  shootout  Turn-based: players alternate, SHOOTOUT_SHOTS each; most makes
            wins; ties go to sudden-death rounds among the tied leaders.
  race      Everyone shoots freely until the buzzer; most points wins.
  horse     H-O-R-S-E. The setter shoots; a make sets the challenge by its kind
            (swish, bank, or any other make: "bucket") and everyone else in turn
            gets one try to match it (a bucket is matched by any make). Missing
            the match earns a letter; five letters and you're out. The setter
            keeps setting until they miss, then the next player sets. Last one
            standing wins.

Trust model: clients send only {power, angle, arc, aim, t, camera}. The server validates
ranges and pacing, decides the wind, re-simulates the shot with the same
deterministic physics the browser uses, and broadcasts the result.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from .game.green import green_launch, is_green
from .game.physics import ShotSim, simulate_shot
from .game.rules import DIFFICULTIES, POWER_RANGE, court_for, roll_wind, score_shot, throw_to_launch

MatchMode = Literal["shootout", "race", "horse"]
Msg = dict[str, Any]

COUNTDOWN_S = 3.0
TURN_S = 20.0  # time to take your shootout shot
AWAY_TURN_S = 8.0  # shorter clock when the shooter is disconnected
COOLDOWN_S = 0.8  # matches the client detector's cooldown
SHOOTOUT_SHOTS = 5
MAX_SUDDEN_DEATH_ROUNDS = 10
HORSE = "HORSE"
# Safety cap: a H-O-R-S-E game this long ends with the fewest letters winning.
HORSE_MAX_SHOTS = 150
TURN_MODES = ("shootout", "horse")
ShotKind = Literal["swish", "bank", "bucket"]
# The client's release timestamp is trusted if it's this close to server time.
RELEASE_TOLERANCE_S = 0.5
PATH_STRIDE = 2  # send every 2nd sample (60 Hz) to keep messages small


class MatchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PlayerStats:
    points: int = 0
    makes: int = 0
    shots: int = 0
    swishes: int = 0
    banks: int = 0
    streak: int = 0
    best: int = 0
    wind: float = 0.0
    last_throw: float = -math.inf
    busy_until: float = -math.inf
    forfeited: bool = False
    letters: int = 0  # H-O-R-S-E

    def public(self) -> dict[str, Any]:
        return {
            "points": self.points,
            "makes": self.makes,
            "shots": self.shots,
            "swishes": self.swishes,
            "banks": self.banks,
            "streak": self.streak,
            "best": self.best,
            "wind": self.wind,
            "forfeited": self.forfeited,
            "letters": self.letters,
        }


@dataclass
class MatchEngine:
    mode: MatchMode
    difficulty: str
    order: list[str]
    now: float
    race_seconds: int = 60
    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        self.diff = DIFFICULTIES[self.difficulty]
        self.court = court_for(self.diff)
        self.start_at = self.now + COUNTDOWN_S
        self.ends_at = self.start_at + self.race_seconds if self.mode == "race" else None
        self.stats = {pid: PlayerStats(wind=roll_wind(self.diff, self.rng)) for pid in self.order}
        self.finished = False
        self.result: Msg | None = None
        # Set by the room once this game has counted towards its series, and once it's been saved.
        self.series_recorded = False
        self.finish_recorded = False
        self.shot_count = 0
        # Shootout state
        self.round = 1
        self.sudden_death = False
        self.sd_group: list[str] = []
        self.sd_makes_before: dict[str, int] = {}
        self.taken: dict[str, int] = {pid: 0 for pid in self.order}
        self.queue: list[str] = list(self.order) if self.mode == "shootout" else []
        # H-O-R-S-E: who sets, and the shot to match (None while setting)
        self.setter: str | None = self.order[0] if self.mode == "horse" else None
        self.challenge: ShotKind | None = None
        if self.mode == "horse":
            self.queue = [self.order[0]]
        self.turn_available_at = self.start_at
        self.deadline = self.start_at + TURN_S

    # ── Queries ──

    @property
    def active(self) -> list[str]:
        return [p for p in self.order if not self.stats[p].forfeited]

    @property
    def alive(self) -> list[str]:
        """Still in the game: not forfeited and (H-O-R-S-E) not spelled out."""
        return [p for p in self.active if self.stats[p].letters < len(HORSE)]

    @property
    def shooter(self) -> str | None:
        return self.queue[0] if self.mode in TURN_MODES and self.queue and not self.finished else None

    def scoreboard(self) -> Msg:
        return {"type": "scoreboard", "scores": {p: s.public() for p, s in self.stats.items()}}

    def turn_msg(self) -> Msg | None:
        pid = self.shooter
        if pid is None:
            return None
        return {
            "type": "turn",
            "playerId": pid,
            "shotsLeft": None if self.sudden_death or self.mode == "horse" else SHOOTOUT_SHOTS - self.taken[pid],
            "round": self.round,
            "suddenDeath": self.sudden_death,
            "setter": self.setter,
            "challenge": self.challenge,
            "availableAt": self.turn_available_at,
            "deadline": self.deadline,
        }

    def start_msg(self) -> Msg:
        return {
            "type": "match_start",
            "mode": self.mode,
            "difficulty": self.difficulty,
            "order": self.order,
            "startAt": self.start_at,
            "endsAt": self.ends_at,
            "raceSeconds": self.race_seconds if self.mode == "race" else None,
            "shootoutShots": SHOOTOUT_SHOTS if self.mode == "shootout" else None,
        }

    def snapshot(self) -> list[Msg]:
        """Everything a (re)connecting player needs to rebuild the match view."""
        msgs = [self.start_msg(), self.scoreboard()]
        if self.result:
            msgs.append(self.result)
        elif (t := self.turn_msg()) is not None:
            msgs.append(t)
        return msgs

    # ── Commands ──

    def throw(self, pid: str, payload: dict[str, Any], now: float) -> tuple[list[Msg], ShotSim]:
        if self.finished:
            raise MatchError("match_over", "The match is over")
        if pid not in self.stats or self.stats[pid].forfeited:
            raise MatchError("not_in_match", "You're not playing in this match")
        if now < self.start_at:
            raise MatchError("not_started", "Wait for the countdown")
        st = self.stats[pid]

        power, angle, arc = (payload.get(k) for k in ("power", "angle", "arc"))
        aim = payload.get("aim", 0)  # sideways throw angle (degrees); older clients don't send it
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (power, angle, arc, aim)):
            raise MatchError("invalid_throw", "Throw needs numeric power, angle, arc and aim")
        if not (POWER_RANGE[0] <= power <= POWER_RANGE[1] and -90 <= angle <= 90 and 0 <= arc <= 1 and -90 <= aim <= 90):
            raise MatchError("invalid_throw", "Throw values out of range")

        if self.mode in TURN_MODES:
            if pid != self.shooter:
                raise MatchError("not_your_turn", "It's not your turn")
            if now < self.turn_available_at:
                raise MatchError("too_fast", "Wait for the last shot to land")
        if now - st.last_throw < COOLDOWN_S or now < st.busy_until:
            raise MatchError("too_fast", "One ball at a time")

        # Trust the client's release time only if it's close to ours (latency), never the future.
        t = payload.get("t")
        release = now
        if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) and now - RELEASE_TOLERANCE_S <= t <= now:
            release = float(t)
        if self.ends_at is not None and release > self.ends_at:
            raise MatchError("time_up", "The buzzer already went")

        launch = throw_to_launch(power, angle, arc, aim)
        wind = st.wind
        phase = release - self.start_at
        # The green rim: a camera throw released while it's green swishes, however it was thrown.
        # ("camera" is the client's word, like the throw itself; keyboard and touch don't get it.)
        green = payload.get("camera") is True and is_green(phase, self.diff.green_window)
        if green:
            launch = green_launch(self.court, wind=wind, phase=phase) or launch
        sim = simulate_shot(launch, self.court, wind=wind, phase=phase)
        points, multiplier, streak = score_shot(sim.result, st.streak, self.diff.score_multiplier)

        st.shots += 1
        st.points += points
        st.makes += int(sim.result.scored)
        st.swishes += int(sim.result.swish)
        st.banks += int(sim.result.bank)
        st.streak = streak
        st.best = max(st.best, streak)
        st.last_throw = now
        st.busy_until = now + sim.duration
        st.wind = roll_wind(self.diff, self.rng)
        self.shot_count += 1

        msgs: list[Msg] = [{**self._shot_msg(pid, sim, points, multiplier, wind, phase, release), "green": green}]
        if self.mode == "shootout":
            self.taken[pid] += 1
            msgs += self._advance_turn(now, after=sim.duration)
        elif self.mode == "horse":
            msgs += self._horse_shot(pid, shot_kind(sim), now, after=sim.duration)
        msgs.append(self.scoreboard())
        if self.finished and self.result:
            msgs.append(self.result)
        return msgs, sim

    def tick(self, now: float, is_connected: Callable[[str], bool]) -> list[Msg]:
        """Turn timeouts (shootout) and the buzzer (race)."""
        if self.finished:
            return []
        msgs: list[Msg] = []
        if self.mode in TURN_MODES:
            pid = self.shooter
            if pid is not None and now >= self.turn_available_at:
                limit = TURN_S if is_connected(pid) else AWAY_TURN_S
                self.deadline = min(self.deadline, self.turn_available_at + limit)
            if pid is not None and now > self.deadline:
                st = self.stats[pid]
                st.shots += 1
                st.streak = 0
                msgs.append({"type": "shot_result", "playerId": pid, "timeout": True, "scored": False, "points": 0})
                if self.mode == "horse":
                    msgs += self._horse_shot(pid, None, now, after=0)
                else:
                    self.taken[pid] += 1
                    msgs += self._advance_turn(now, after=0)
                msgs.append(self.scoreboard())
        elif self.ends_at is not None:
            in_flight = max((s.busy_until for s in self.stats.values()), default=-math.inf)
            if now >= self.ends_at and now >= in_flight:
                self._finish("time")
        if self.finished and self.result:
            msgs.append(self.result)
        return msgs

    def forfeit(self, pid: str, now: float) -> list[Msg]:
        if self.finished or pid not in self.stats or self.stats[pid].forfeited:
            return []
        self.stats[pid].forfeited = True
        msgs: list[Msg] = [{"type": "forfeit", "playerId": pid}]
        was_shooter = self.shooter == pid
        self.queue = [p for p in self.queue if p != pid]
        self.sd_group = [p for p in self.sd_group if p != pid]
        if len(self.active) <= 1:
            self._finish("forfeit")
        elif self.mode == "horse":
            if len(self.alive) <= 1:
                self._finish("complete")
            elif pid == self.setter:
                msgs += self._horse_next_setter(now, after_pid=pid)
            elif not self.queue:
                msgs += self._horse_set_again(now)
            elif was_shooter:
                self._open_turn(now)
                if (t := self.turn_msg()) is not None:
                    msgs.append(t)
        elif self.mode == "shootout":
            if not self.queue:
                msgs += self._round_complete(now)
            elif was_shooter:
                self._open_turn(now)
                if (t := self.turn_msg()) is not None:
                    msgs.append(t)
        msgs.append(self.scoreboard())
        if self.finished and self.result:
            msgs.append(self.result)
        return msgs

    def abort(self) -> Msg:
        self._finish("aborted")
        assert self.result
        return self.result

    # ── Shootout turn flow ──

    def _open_turn(self, now: float, after: float = 0.0) -> None:
        self.turn_available_at = max(now + after, self.start_at)
        self.deadline = self.turn_available_at + TURN_S

    def _advance_turn(self, now: float, after: float) -> list[Msg]:
        self.queue.pop(0)
        if not self.queue:
            return self._round_complete(now, after)
        self._open_turn(now, after)
        t = self.turn_msg()
        return [t] if t else []

    def _round_complete(self, now: float, after: float = 0.0) -> list[Msg]:
        if not self.sudden_death:
            if self.round < SHOOTOUT_SHOTS:
                self.round += 1
                self.queue = list(self.active)
            else:
                best = max(self.stats[p].makes for p in self.active)
                leaders = [p for p in self.active if self.stats[p].makes == best]
                if len(leaders) == 1:
                    self._finish("complete")
                    return []
                self.sudden_death = True
                self.round = 1
                self.sd_group = leaders
                self.sd_makes_before = {p: self.stats[p].makes for p in leaders}
                self.queue = list(leaders)
        else:
            made = [p for p in self.sd_group if self.stats[p].makes > self.sd_makes_before[p]]
            if 0 < len(made) < len(self.sd_group):
                self.sd_group = made
            if len(self.sd_group) == 1:
                self._finish("complete")
                return []
            if self.round >= MAX_SUDDEN_DEATH_ROUNDS:
                self._finish("complete")  # still tied: shared win
                return []
            self.round += 1
            self.sd_makes_before = {p: self.stats[p].makes for p in self.sd_group}
            self.queue = list(self.sd_group)
        self._open_turn(now, after)
        t = self.turn_msg()
        return [t] if t else []

    # ── H-O-R-S-E turn flow ──

    def _horse_shot(self, pid: str, kind: ShotKind | None, now: float, after: float) -> list[Msg]:
        """`kind` is what the shot was (None = a miss or a timeout)."""
        msgs: list[Msg] = []
        if self.challenge is None:
            # Setting: a make becomes the challenge for everyone else, in order after the setter.
            if kind is None:
                return self._horse_next_setter(now, after_pid=pid, after=after)
            self.challenge = kind
            i = self.order.index(pid)
            others = self.order[i + 1 :] + self.order[:i]
            self.queue = [p for p in others if p in self.alive]
            msgs.append({"type": "challenge", "playerId": pid, "challenge": kind})
        else:
            # Matching: miss the match, get a letter.
            matched = kind is not None and (self.challenge == "bucket" or kind == self.challenge)
            if not matched:
                st = self.stats[pid]
                st.letters += 1
                msgs.append({"type": "letter", "playerId": pid, "letters": st.letters, "word": HORSE[: st.letters], "out": st.letters >= len(HORSE)})
            self.queue.pop(0)
            if len(self.alive) <= 1:
                self._finish("complete")
                return msgs
            if not self.queue:
                return msgs + self._horse_set_again(now, after)
        if self.shot_count >= HORSE_MAX_SHOTS:
            self._finish("complete")
            return msgs
        self._open_turn(now, after)
        if (t := self.turn_msg()) is not None:
            msgs.append(t)
        return msgs

    def _horse_set_again(self, now: float, after: float = 0.0) -> list[Msg]:
        """Everyone has tried the match: the setter sets again (or the next player, if the setter left)."""
        self.challenge = None
        if self.setter not in self.alive:
            return self._horse_next_setter(now, after_pid=self.setter or self.order[0], after=after)
        self.queue = [self.setter]
        self.round += 1
        return self._horse_open(now, after)

    def _horse_next_setter(self, now: float, after_pid: str, after: float = 0.0) -> list[Msg]:
        self.challenge = None
        alive = self.alive
        if len(alive) <= 1:
            self._finish("complete")
            return []
        i = self.order.index(after_pid)
        self.setter = next(p for p in self.order[i + 1 :] + self.order[: i + 1] if p in alive)
        self.queue = [self.setter]
        self.round += 1
        return self._horse_open(now, after)

    def _horse_open(self, now: float, after: float) -> list[Msg]:
        if self.shot_count >= HORSE_MAX_SHOTS:
            self._finish("complete")
            return []
        self._open_turn(now, after)
        t = self.turn_msg()
        return [t] if t else []

    # ── Results ──

    def _standings(self) -> list[dict[str, Any]]:
        def key(pid: str):
            s = self.stats[pid]
            in_sd = pid in self.sd_group if self.sudden_death else True
            if self.mode == "shootout":
                return (not s.forfeited, in_sd, s.makes, s.points)
            if self.mode == "horse":
                return (not s.forfeited, -s.letters, s.makes)
            return (not s.forfeited, s.points, s.makes)

        ranked = sorted(self.order, key=key, reverse=True)
        rows, last_key, rank = [], None, 0
        for i, pid in enumerate(ranked):
            k = key(pid)
            if k != last_key:
                rank, last_key = i + 1, k
            rows.append({"playerId": pid, "rank": rank, **self.stats[pid].public()})
        return rows

    def _finish(self, reason: str) -> None:
        if self.finished:
            return
        self.finished = True
        standings = self._standings()
        winners = [] if reason == "aborted" else [r["playerId"] for r in standings if r["rank"] == 1 and not r["forfeited"]]
        self.result = {
            "type": "match_end",
            "winner": winners[0] if len(winners) == 1 else None,
            "winners": winners,
            "standings": standings,
            "reason": reason,
        }

    def _shot_msg(self, pid: str, sim: ShotSim, points: int, multiplier: float, wind: float, phase: float, release: float) -> Msg:
        r = sim.result
        return {
            "type": "shot_result",
            "playerId": pid,
            "shotId": self.shot_count,
            "scored": r.scored,
            "swish": r.swish,
            "bank": r.bank,
            "miss": r.miss,
            "error": r.error,
            "lateral": r.lateral,
            "points": points,
            "multiplier": multiplier,
            "streak": self.stats[pid].streak,
            "launch": {"speed": sim.launch.speed, "angle": sim.launch.angle, "aim": sim.launch.aim},
            "wind": wind,
            "phase": phase,
            "releaseAt": release,
            "duration": sim.duration,
            "path": [list(p) for p in sim.path[::PATH_STRIDE]] + ([list(sim.path[-1])] if (len(sim.path) - 1) % PATH_STRIDE else []),
            "events": [{"t": e.t, "type": e.type, "x": e.x, "y": e.y, "z": e.z, "impact": e.impact} for e in sim.events],
        }


def shot_kind(sim: ShotSim) -> ShotKind | None:
    """What a H-O-R-S-E shot was: None for a miss."""
    r = sim.result
    if not r.scored:
        return None
    return "swish" if r.swish else "bank" if r.bank else "bucket"
