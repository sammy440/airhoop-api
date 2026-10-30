from datetime import date, timedelta

import pytest

from app.game import daily
from app.game.daily import daily_rules, mix32, parse_key

from .test_api import make_guest, score

# Same dates and answers as daily.test.ts in the frontend: if either side changes, both tests fail.
PINNED = [
    (date(2026, 10, 30), 3728926168, "starter", 60),
    (date(2026, 10, 31), 2715940137, "allstar", 60),
    (date(2027, 1, 1), 3536523236, "starter", 90),
]


@pytest.mark.parametrize(("day", "seed", "difficulty", "seconds"), PINNED)
def test_rules_match_the_frontend(day, seed, difficulty, seconds):
    assert mix32((day - date(1970, 1, 1)).days) == seed
    rules = daily_rules(day)
    assert (rules.difficulty, rules.seconds) == (difficulty, seconds)
    assert rules.key == f"daily{day:%Y%m%d}"


def test_parse_key():
    assert parse_key("daily20261030") == date(2026, 10, 30)
    assert parse_key("daily20261399") is None


def test_daily_scores(client, monkeypatch):
    monkeypatch.setattr(daily, "today", lambda: date(2026, 10, 30))  # starter · 60 s
    t, _ = make_guest(client, "Daily")
    assert score(client, t, mode="daily20261030", difficulty="starter", points=30, makes=10, shots=20).status_code == 201
    # Just after midnight: yesterday's run still posts to yesterday's board.
    assert score(client, t, mode="daily20261029", difficulty=daily_rules(date(2026, 10, 29)).difficulty, points=2, makes=1, shots=1).status_code == 201
    for bad in [
        dict(difficulty="legend"),  # not today's difficulty
        dict(shots=61),  # more shots than the 60 s clock allows
        dict(mode="daily20261027"),  # an old challenge
        dict(mode="daily20261399"),  # not a date
    ]:
        args = dict(mode="daily20261030", difficulty="starter", points=2, makes=1, shots=1) | bad
        assert score(client, t, **args).status_code == 422, bad

    board = client.get("/leaderboard", params={"mode": "daily20261030"}).json()
    assert [(e["nickname"], e["best"]) for e in board] == [("Daily", 30)]
    assert client.get("/leaderboard", params={"mode": "daily2026"}).status_code == 422


def test_every_day_has_valid_rules():
    for i in range(400):
        rules = daily_rules(date(2026, 1, 1) + timedelta(days=i))
        assert rules.difficulty in ("starter", "allstar", "legend")
        assert 30 <= rules.seconds <= 90
