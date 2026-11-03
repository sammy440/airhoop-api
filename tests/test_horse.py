"""H-O-R-S-E rules (pure logic, injected time)."""

import random

import pytest

from app.game.physics import simulate_shot
from app.game.rules import DIFFICULTIES, court_for, throw_to_launch
from app.match import COUNTDOWN_S, HORSE_MAX_SHOTS, TURN_S, MatchEngine, MatchError, shot_kind

T0 = 1000.0
START = T0 + COUNTDOWN_S
COURT = court_for(DIFFICULTIES["starter"])  # still hoop, no wind


def find(kind):
    """A throw that comes out as `kind` ("swish" / "bank" / "bucket" / None for a miss) on the starter court."""
    for arc in (0.7, 0.4, 0.95, 0.2):
        for p in range(200):
            payload = {"power": p / 200, "angle": 30, "arc": arc}
            sim = simulate_shot(throw_to_launch(p / 200, 30, arc), COURT)
            if shot_kind(sim) == kind and (kind is not None or not sim.result.scored):
                return payload
    raise AssertionError(f"no {kind} throw")


SWISH, BANK, BUCKET, MISS = find("swish"), find("bank"), find("bucket"), {"power": 0.05, "angle": 30, "arc": 0.7}


def horse(players=("a", "b")):
    return MatchEngine("horse", "starter", list(players), T0, rng=random.Random(1))


class Clock:
    def __init__(self):
        self.now = START + 0.1

    def shoot(self, m, pid, payload):
        msgs, sim = m.throw(pid, payload, self.now)
        self.now += sim.duration + 0.9
        return msgs


def test_setter_shoots_first_and_turns_are_enforced():
    m = horse()
    t = m.turn_msg()
    assert t["playerId"] == "a" and t["setter"] == "a" and t["challenge"] is None and t["shotsLeft"] is None
    with pytest.raises(MatchError) as e:
        m.throw("b", SWISH, START + 0.5)
    assert e.value.code == "not_your_turn"


def test_a_make_sets_the_challenge_and_a_failed_match_earns_a_letter():
    m, c = horse(), Clock()
    msgs = c.shoot(m, "a", BANK)
    assert {"type": "challenge", "playerId": "a", "challenge": "bank"} in msgs
    turn = next(x for x in msgs if x["type"] == "turn")
    assert turn["playerId"] == "b" and turn["challenge"] == "bank"
    msgs = c.shoot(m, "b", SWISH)  # a make, but not a bank
    letter = next(x for x in msgs if x["type"] == "letter")
    assert letter == {"type": "letter", "playerId": "b", "letters": 1, "word": "H", "out": False}
    # Everyone has tried: the setter sets again.
    assert m.shooter == "a" and m.challenge is None


def test_a_bucket_is_matched_by_any_make_and_a_swish_only_by_a_swish():
    m, c = horse(), Clock()
    c.shoot(m, "a", BUCKET)
    assert not any(x["type"] == "letter" for x in c.shoot(m, "b", SWISH))
    c.shoot(m, "a", SWISH)
    assert any(x["type"] == "letter" for x in c.shoot(m, "b", BUCKET))
    assert m.stats["b"].letters == 1


def test_a_missed_set_passes_the_ball():
    m, c = horse(("a", "b", "c")), Clock()
    msgs = c.shoot(m, "a", MISS)
    assert not any(x["type"] == "letter" for x in msgs)
    assert m.setter == "b" and m.shooter == "b" and m.challenge is None


def test_matchers_go_in_order_after_the_setter():
    m, c = horse(("a", "b", "c")), Clock()
    c.shoot(m, "a", MISS)  # b sets
    c.shoot(m, "b", SWISH)
    assert m.queue == ["c", "a"]


def test_five_letters_and_youre_out():
    m, c = horse(), Clock()
    for i in range(5):
        c.shoot(m, "a", SWISH)
        msgs = c.shoot(m, "b", MISS)
    letter = next(x for x in msgs if x["type"] == "letter")
    assert letter["word"] == "HORSE" and letter["out"] is True
    end = msgs[-1]
    assert end["type"] == "match_end" and end["winner"] == "a" and end["reason"] == "complete"
    assert [r["letters"] for r in end["standings"]] == [0, 5]


def test_spelled_out_players_are_skipped():
    m, c = horse(("a", "b", "c")), Clock()
    m.stats["b"].letters = 5
    c.shoot(m, "a", SWISH)
    assert m.queue == ["c"]


def test_a_timeout_on_a_match_is_a_letter():
    m, c = horse(), Clock()
    c.shoot(m, "a", SWISH)
    m.tick(c.now + TURN_S + 0.1, lambda _: True)
    assert m.stats["b"].letters == 1 and m.shooter == "a"


def test_the_setter_leaving_passes_the_set():
    m, c = horse(("a", "b", "c")), Clock()
    c.shoot(m, "a", SWISH)
    m.forfeit("a", c.now)
    assert not m.finished and m.setter == "b" and m.challenge is None and m.shooter == "b"


def test_a_very_long_game_ends_with_fewest_letters_winning():
    m, c = horse(), Clock()
    m.stats["b"].letters = 2
    m.shot_count = HORSE_MAX_SHOTS - 1
    msgs = c.shoot(m, "a", MISS)
    assert m.finished and msgs[-1]["winner"] == "a"
