"""Match engine rules (pure logic, injected time)."""

import math
import random

import pytest

from app.game.physics import simulate_shot
from app.game.rules import DIFFICULTIES, court_for, throw_to_launch
from app.match import AWAY_TURN_S, COUNTDOWN_S, COOLDOWN_S, SHOOTOUT_SHOTS, TURN_S, MatchEngine, MatchError

T0 = 1000.0
START = T0 + COUNTDOWN_S


def scoring_power(difficulty: str = "starter") -> float:
    """A power that swishes with a still hoop and no wind (angle 30, arc 0.7 → 52° launch)."""
    court = court_for(DIFFICULTIES[difficulty])
    hits = [p / 200 for p in range(200) if simulate_shot(throw_to_launch(p / 200, 30, 0.7), court).result.swish]
    return hits[len(hits) // 2]


MAKE = {"power": scoring_power(), "angle": 30, "arc": 0.7}
MISS = {"power": 0.05, "angle": 30, "arc": 0.7}


def race(players=("a", "b"), seconds=60, difficulty="starter"):
    return MatchEngine("race", difficulty, list(players), T0, race_seconds=seconds, rng=random.Random(1))


def shootout(players=("a", "b")):
    return MatchEngine("shootout", "starter", list(players), T0, rng=random.Random(1))


def types(msgs):
    return [m["type"] for m in msgs]


# ── Validation / anti-cheat ──


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"power": "0.5", "angle": 30, "arc": 0.7},
        {"power": math.nan, "angle": 30, "arc": 0.7},
        {"power": math.inf, "angle": 30, "arc": 0.7},
        {"power": True, "angle": 30, "arc": 0.7},
        {"power": 5, "angle": 30, "arc": 0.7},
        {"power": -1, "angle": 30, "arc": 0.7},
        {"power": 0.4, "angle": 200, "arc": 0.7},
        {"power": 0.4, "angle": 30, "arc": 1.5},
    ],
)
def test_rejects_impossible_throws(payload):
    m = race()
    with pytest.raises(MatchError) as e:
        m.throw("a", payload, START + 1)
    assert e.value.code == "invalid_throw"


def test_rejects_throws_before_start_from_strangers_and_too_fast():
    m = race()
    with pytest.raises(MatchError, match="countdown"):
        m.throw("a", MAKE, START - 0.1)
    with pytest.raises(MatchError) as e:
        m.throw("zed", MAKE, START + 1)
    assert e.value.code == "not_in_match"
    m.throw("a", MAKE, START + 1)
    with pytest.raises(MatchError) as e:
        m.throw("a", MAKE, START + 1 + COOLDOWN_S / 2)
    assert e.value.code == "too_fast"
    with pytest.raises(MatchError) as e:  # past the cooldown but the ball is still in the air
        m.throw("a", MAKE, START + 1 + COOLDOWN_S + 0.01)
    assert e.value.code == "too_fast"


def test_server_result_matches_physics_and_scoring():
    m = race()
    msgs, sim = m.throw("a", MAKE, START + 1)
    shot = msgs[0]
    assert shot["type"] == "shot_result" and shot["scored"] and shot["swish"] and shot["points"] == 3
    again = simulate_shot(throw_to_launch(**MAKE), m.court, wind=0, phase=shot["phase"])
    assert again.result == sim.result
    assert shot["path"][0] == [0.0, 0.0, 2.1, 0.0] and shot["path"][-1] == list(sim.path[-1])
    assert m.stats["a"].points == 3 and m.stats["a"].makes == 1
    assert types(msgs) == ["shot_result", "scoreboard"]


def test_release_time_is_trusted_only_within_tolerance():
    m = race()
    now = START + 2
    assert m.throw("a", {**MAKE, "t": now - 0.2}, now)[0][0]["releaseAt"] == pytest.approx(now - 0.2)
    later = now + 5
    assert m.throw("a", {**MAKE, "t": later - 3}, later)[0][0]["releaseAt"] == later  # too old: ignored
    later2 = later + 5
    assert m.throw("a", {**MAKE, "t": later2 + 1}, later2)[0][0]["releaseAt"] == later2  # future: ignored


def test_server_decides_the_wind():
    from app.game.rules import roll_wind

    m = race(difficulty="legend")
    expected = random.Random(1)  # same seed as the engine: winds for a, b, then a's next shot
    wind_a, _wind_b, next_a = (roll_wind(DIFFICULTIES["legend"], expected) for _ in range(3))
    assert m.stats["a"].wind == wind_a
    shot = m.throw("a", {**MAKE, "wind": 99}, START + 1)[0][0]  # a client-supplied wind is ignored
    assert shot["wind"] == wind_a
    assert m.stats["a"].wind == next_a


# ── Race ──


def test_race_buzzer_and_winner():
    m = race(seconds=30)
    t = START
    for _ in range(3):
        t += 3
        m.throw("a", MAKE, t)
    m.throw("b", MAKE, START + 2)
    end = START + 30
    with pytest.raises(MatchError) as e:
        m.throw("b", MAKE, end + 0.1)
    assert e.value.code == "time_up"
    assert m.tick(end - 1, lambda _: True) == []
    done = m.tick(end + 0.01, lambda _: True)
    assert done[-1]["type"] == "match_end"
    assert done[-1]["winner"] == "a" and done[-1]["reason"] == "time"
    assert [r["playerId"] for r in done[-1]["standings"]] == ["a", "b"]


def test_buzzer_beater_counts_and_match_waits_for_it():
    m = race(seconds=30)
    end = START + 30
    msgs, sim = m.throw("a", {**MAKE, "t": end - 0.1}, end + 0.2)  # released before the buzzer, arrived after
    assert msgs[0]["scored"]
    assert m.tick(end + 0.3, lambda _: True) == []  # ball still in the air
    assert m.tick(end + 0.2 + sim.duration + 0.01, lambda _: True)[-1]["winner"] == "a"


def test_race_tie_is_shared():
    m = race(seconds=30)
    m.throw("a", MAKE, START + 1)
    m.throw("b", MAKE, START + 1)
    end = m.tick(START + 31, lambda _: True)[-1]
    assert end["winner"] is None and set(end["winners"]) == {"a", "b"}


# ── Shootout ──


def play_round(m, now, makes):
    """Each player in turn order shoots; `makes` maps player → bool."""
    for _ in range(len(m.queue)):
        pid = m.shooter
        now = max(now, m.turn_available_at) + 0.01
        msgs, _ = m.throw(pid, MAKE if makes[pid] else MISS, now)
    return now, msgs


def test_shootout_turns_alternate_and_enforce_order():
    m = shootout()
    first = m.turn_msg()
    assert first["playerId"] == "a" and first["shotsLeft"] == SHOOTOUT_SHOTS
    with pytest.raises(MatchError) as e:
        m.throw("b", MAKE, START + 1)
    assert e.value.code == "not_your_turn"
    msgs, sim = m.throw("a", MAKE, START + 1)
    turn = next(x for x in msgs if x["type"] == "turn")
    assert turn["playerId"] == "b" and turn["availableAt"] == pytest.approx(START + 1 + sim.duration)
    with pytest.raises(MatchError) as e:  # next shooter waits for the ball to land
        m.throw("b", MAKE, START + 1.2)
    assert e.value.code == "too_fast"


def test_shootout_regulation_winner():
    m = shootout()
    now = START
    for r in range(SHOOTOUT_SHOTS):
        now, msgs = play_round(m, now, {"a": True, "b": r < 2})
    end = msgs[-1]
    assert end["type"] == "match_end" and end["winner"] == "a" and end["reason"] == "complete"
    assert m.stats["a"].makes == 5 and m.stats["b"].makes == 2


def test_shootout_tie_goes_to_sudden_death():
    m = shootout()
    now = START
    for _ in range(SHOOTOUT_SHOTS):
        now, msgs = play_round(m, now, {"a": True, "b": True})
    assert not m.finished and m.sudden_death
    assert next(x for x in reversed(msgs) if x["type"] == "turn")["suddenDeath"] is True
    now, msgs = play_round(m, now, {"a": True, "b": True})  # both make: still tied
    assert not m.finished
    now, msgs = play_round(m, now, {"a": False, "b": True})
    assert m.finished and msgs[-1]["winner"] == "b"


def test_shootout_turn_timeout_counts_as_a_miss():
    m = shootout()
    assert m.tick(START + TURN_S - 1, lambda _: True) == []
    msgs = m.tick(START + TURN_S + 0.1, lambda _: True)
    assert msgs[0] == {"type": "shot_result", "playerId": "a", "timeout": True, "scored": False, "points": 0}
    assert m.shooter == "b" and m.stats["a"].shots == 1


def test_disconnected_shooter_gets_a_shorter_clock():
    m = shootout()
    assert m.tick(START + AWAY_TURN_S + 0.1, lambda pid: pid != "a")[0]["timeout"] is True


def test_forfeit_ends_a_two_player_match():
    m = shootout()
    m.throw("a", MISS, START + 1)
    msgs = m.forfeit("b", START + 2)
    end = msgs[-1]
    assert end["type"] == "match_end" and end["winner"] == "a" and end["reason"] == "forfeit"


def test_forfeit_in_three_player_match_continues():
    m = shootout(("a", "b", "c"))
    msgs = m.forfeit("a", START + 0.5)  # the current shooter leaves
    assert not m.finished
    assert next(x for x in msgs if x["type"] == "turn")["playerId"] == "b"
    assert m.queue == ["b", "c"]


def test_abort_has_no_winner():
    m = race()
    end = m.abort()
    assert end["reason"] == "aborted" and end["winners"] == []


def test_aim_steers_the_shot_and_is_validated():
    m = race()
    wide = m.throw("a", {**MAKE, "aim": 40}, START + 1)[0][0]
    assert not wide["scored"] and wide["lateral"] > 0.2 and wide["launch"]["aim"] > 0
    for bad in (91, "left", None):
        with pytest.raises(MatchError) as e:
            m.throw("b", {**MAKE, "aim": bad}, START + 1)
        assert e.value.code == "invalid_throw"
    # Older clients send no aim: straight.
    assert m.throw("b", MAKE, START + 1)[0][0]["launch"]["aim"] == 0
