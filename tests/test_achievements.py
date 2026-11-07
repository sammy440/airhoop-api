"""Achievements: the rules, totals from /xp, dailies in a row, refereed match awards, and claims."""

import uuid
from datetime import timedelta

from sqlmodel import Session

from app.game import achievements as rules
from app.game import daily
from app.match import COUNTDOWN_S
from app.models import PlayerDay

from .conftest import auth, make_guest, receive_until
from .test_match import MAKE
from .test_match_ws import connect, lobby, start_match  # noqa: F401 - lobby is a fixture

# Same ids, in the same order, as the frontend's ACHIEVEMENTS (src/lib/game/achievements.ts)
IDS = [
    "first_bucket", "swish_10", "swish_100", "bank_10", "shots_1000", "shots_10000", "streak_5", "streak_10", "streak_20",
    "daily_1", "daily_7", "first_win", "horse_clean", "shootout_perfect", "series_win", "buzzer_beater", "green_light", "world_tour",
]


def ids(client, token):
    return [a["id"] for a in client.get("/me/achievements", headers=auth(token)).json()]


def test_catalog_matches_the_frontend():
    assert list(rules.ALL) == IDS
    assert all(len(a) <= 24 for a in IDS)


def test_longest_run_of_dailies():
    assert rules.longest_run([]) == 0
    assert rules.longest_run(["20261101", "20261103", "20261104", "20261105", "20261104"]) == 3
    assert rules.longest_run(["20261231", "20270101"]) == 2


def test_match_rules():
    win = {"forfeited": False, "letters": 0, "shots": 5, "makes": 5}
    assert rules.from_match("shootout", win, won=True, series_won=False) == {"first_win", "shootout_perfect"}
    assert rules.from_match("shootout", win | {"makes": 4}, won=True, series_won=True) == {"first_win", "series_win"}
    assert rules.from_match("horse", win, won=True, series_won=False) == {"first_win", "horse_clean"}
    assert rules.from_match("horse", win | {"letters": 2}, won=True, series_won=False) == {"first_win"}
    assert rules.from_match("race", win, won=False, series_won=False) == set()


def test_xp_syncs_unlock_totals_once(client):
    token, _ = make_guest(client, "Ayo")
    r = client.post("/xp", json={"shots": 12, "makes": 10, "swishes": 6, "banks": 4, "best_streak": 6}, headers=auth(token)).json()
    assert r["achievements"] == ["first_bucket", "streak_5"]
    r = client.post("/xp", json={"shots": 12, "makes": 10, "swishes": 4, "banks": 6, "best_streak": 3}, headers=auth(token)).json()
    assert r["achievements"] == ["bank_10", "swish_10"]  # totals add up across syncs; nothing twice
    assert set(ids(client, token)) == {"first_bucket", "streak_5", "bank_10", "swish_10"}


def test_daily_challenges_in_a_row(client, engine):
    token, uid = make_guest(client, "Ayo")
    today = daily.today()
    r = client.post("/xp", json={"runs": 1, "daily": f"daily{today:%Y%m%d}"}, headers=auth(token)).json()
    assert r["achievements"] == ["daily_1"]
    # Six earlier days already in the history: today makes seven in a row
    with Session(engine) as s:
        for back in range(1, 7):
            s.add(PlayerDay(user_id=uuid.UUID(uid), day=f"{today - timedelta(days=back):%Y%m%d}", source="solo", daily=True))
        s.commit()
    r = client.post("/xp", json={"shots": 1, "makes": 0}, headers=auth(token)).json()
    assert r["achievements"] == ["daily_7"]


def test_claiming_solo_moments(client):
    token, _ = make_guest(client, "Ayo")
    first = client.post("/me/achievements/buzzer_beater", headers=auth(token))
    assert first.status_code == 200 and first.json()["new"] is True
    again = client.post("/me/achievements/buzzer_beater", headers=auth(token)).json()
    assert again["new"] is False and again["unlocked_at"] == first.json()["unlocked_at"]
    # The server's own achievements can't be claimed
    assert client.post("/me/achievements/first_win", headers=auth(token)).status_code == 422
    assert client.post("/me/achievements/nonsense", headers=auth(token)).status_code == 422
    assert client.post("/me/achievements/green_light").status_code == 401
    assert ids(client, token) == ["buzzer_beater"]


def test_winning_a_match_is_refereed(client, lobby, clock):  # noqa: F811
    room = lobby("race")
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_match(a, b)
        clock.advance(COUNTDOWN_S + 0.5)
        a.send_json({"type": "throw", **MAKE, "t": clock.t})
        receive_until(a, "shot_result")
        clock.advance(31)
        client.portal.call(client.app.state.rooms.tick_matches)
        receive_until(a, "match_end")
        assert receive_until(a, "achievements", limit=15)["ids"] == ["first_bucket", "first_win"]
        receive_until(b, "xp", limit=15)
    assert set(ids(client, room["host"])) == {"first_bucket", "first_win"}
    assert ids(client, room["guest"]) == []


def test_erasing_yourself_removes_your_achievements(client):
    token, _ = make_guest(client, "Ayo")
    client.post("/me/achievements/world_tour", headers=auth(token))
    assert client.delete("/auth/me", headers=auth(token)).status_code == 204
