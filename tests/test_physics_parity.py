"""
The server re-simulates every multiplayer throw, so it must agree with the
browser. Fixtures come from the TypeScript implementation
(`GEN_PARITY=1 npx vitest run src/lib/game/parity.gen.test.ts` in the frontend).
"""

import json
from pathlib import Path

import pytest

from app.game.physics import simulate_shot
from app.game.rules import DIFFICULTIES, court_for, score_shot, throw_to_launch

CASES = json.loads((Path(__file__).parent / "fixtures" / "physics_parity.json").read_text())["cases"]


def test_fixture_covers_every_difficulty_and_outcome():
    assert len(CASES) >= 400
    assert {c["input"]["difficulty"] for c in CASES} == set(DIFFICULTIES)
    outcomes = {("swish" if c["result"]["swish"] else "make") if c["result"]["scored"] else c["result"]["miss"] for c in CASES}
    assert {"swish", "make", "short", "long", "rim", "airball"} <= outcomes


@pytest.mark.parametrize("case", CASES, ids=lambda c: "{difficulty}-p{power}-a{angle}-w{wind}".format(**c["input"]))
def test_same_shot_as_the_browser(case):
    i = case["input"]
    d = DIFFICULTIES[i["difficulty"]]
    launch = throw_to_launch(i["power"], i["angle"], i["arc"])
    assert launch.speed == pytest.approx(case["launch"]["speed"], abs=1e-12)
    assert launch.angle == pytest.approx(case["launch"]["angle"], abs=1e-12)

    sim = simulate_shot(launch, court_for(d), wind=i["wind"], phase=i["phase"])
    r = case["result"]
    # Outcomes must match exactly: this is what decides points.
    assert (sim.result.scored, sim.result.swish, sim.result.bank, sim.result.miss) == (r["scored"], r["swish"], r["bank"], r["miss"])
    assert sim.result.error == pytest.approx(r["error"], abs=1e-6)
    assert sim.duration == pytest.approx(case["duration"], abs=1e-9)
    assert [(e.type, round(e.t, 9)) for e in sim.events] == [(e["type"], round(e["t"], 9)) for e in case["events"]]
    assert len(sim.path) == case["pathLength"]
    for (t, x, y), (et, ex, ey) in zip(sim.path[::15], case["path"], strict=True):
        assert (t, x, y) == pytest.approx((et, ex, ey), abs=2e-4)
    assert [score_shot(sim.result, streak, d.score_multiplier)[0] for streak in (0, 4, 11)] == case["points"]
