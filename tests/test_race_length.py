"""Race lengths: the presets or whole minutes up to 30, like the solo shot clock."""

import pytest

from app.match import COUNTDOWN_S, valid_race_seconds

from .conftest import auth, make_guest
from .test_match import T0, race


@pytest.mark.parametrize("s", [30, 60, 90, 120, 180, 300, 600, 1800])
def test_presets_and_whole_minutes_are_races(s):
    assert valid_race_seconds(s)


@pytest.mark.parametrize("s", [0, 29, 45, 61, 150, 1860, 3600, 60.0, "60", True, None])
def test_anything_else_is_not(s):
    assert not valid_race_seconds(s)


def test_rooms_take_a_custom_length(client):
    host, _ = make_guest(client, "Long")
    res = client.post("/rooms", json={"mode": "race", "race_seconds": 600}, headers=auth(host))
    assert res.status_code == 201 and res.json()["race_seconds"] == 600
    assert client.post("/rooms", json={"mode": "race", "race_seconds": 75}, headers=auth(host)).status_code == 422
    assert client.get("/leaderboard", params={"mode": "race1800"}).status_code == 200


def test_a_ten_minute_race_runs_ten_minutes():
    m = race(seconds=600)
    assert m.ends_at == pytest.approx(T0 + COUNTDOWN_S + 600)
