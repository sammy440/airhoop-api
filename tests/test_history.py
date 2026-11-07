"""Match history and shooting trends: GET /me/matches, GET /me/stats, and what feeds them."""

import uuid
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.match import COUNTDOWN_S
from app.models import MatchPlayer, PlayerDay, Throw

from .conftest import auth, make_guest, receive_until
from .test_match import MAKE, MISS
from .test_match_ws import connect, lobby, start_match  # noqa: F401 - lobby is a fixture

TODAY = datetime.now(UTC).strftime("%Y-%m-%d")


def play_race(client, room, clock):
    """Host makes one, guest misses one, the clock runs out. Returns once XP (written last) has arrived."""
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_match(a, b)
        clock.advance(COUNTDOWN_S + 0.5)
        a.send_json({"type": "throw", **MAKE, "t": clock.t})
        receive_until(a, "shot_result")
        clock.advance(1)
        b.send_json({"type": "throw", **MISS, "t": clock.t})
        receive_until(b, "shot_result", playerId=room["guest_id"])
        clock.advance(31)
        client.portal.call(client.app.state.rooms.tick_matches)
        receive_until(a, "match_end")
        receive_until(a, "xp", limit=15)
        receive_until(b, "xp", limit=15)


def test_a_finished_match_is_in_both_players_history(client, lobby, clock, engine):  # noqa: F811
    room = lobby("race")
    play_race(client, room, clock)

    host = client.get("/me/matches", headers=auth(room["host"])).json()
    assert len(host) == 1
    m = host[0]
    assert (m["mode"], m["difficulty"], m["race_seconds"], m["reason"]) == ("race", "starter", 30, "time")
    assert m["you"]["user_id"] == room["host_id"] and m["you"]["won"] and m["you"]["rank"] == 1
    assert (m["you"]["shots"], m["you"]["makes"], m["you"]["points"]) == (1, 1, 3)
    assert [p["nickname"] for p in m["players"]] == ["Host", "Guest"]  # best first
    assert m["players"][1]["won"] is False and m["players"][1]["makes"] == 0

    guest = client.get("/me/matches", headers=auth(room["guest"])).json()
    assert guest[0]["id"] == m["id"] and guest[0]["you"]["user_id"] == room["guest_id"] and not guest[0]["you"]["won"]

    # Someone else sees none of it
    other, _ = make_guest(client, "Other")
    assert client.get("/me/matches", headers=auth(other)).json() == []


def test_throws_keep_the_refereed_detail(client, lobby, clock, engine):  # noqa: F811
    room = lobby("race")
    play_race(client, room, clock)
    with Session(engine) as s:
        made = s.exec(select(Throw).where(Throw.user_id == uuid.UUID(room["host_id"]))).one()
        missed = s.exec(select(Throw).where(Throw.user_id == uuid.UUID(room["guest_id"]))).one()
    assert made.scored and made.swish is True and made.miss is None and made.green is False and made.aim == 0
    assert not missed.scored and missed.miss in {"short", "airball"} and missed.depth_error < 0
    assert missed.side_error is not None


def test_match_shots_count_in_the_stats(client, lobby, clock, engine):  # noqa: F811
    room = lobby("race")
    play_race(client, room, clock)

    host = client.get("/me/stats", headers=auth(room["host"])).json()
    assert host["match"] == {"shots": 1, "makes": 1, "swishes": 1, "banks": 0, "misses": {}}
    assert host["solo"]["shots"] == 0
    assert host["by_day"] == [{"day": TODAY, "solo": None, "match": host["match"]}]
    assert host["record"] == {"race": {"played": 1, "wins": 1}}
    assert host["miss_bias"] is None

    guest = client.get("/me/stats", headers=auth(room["guest"])).json()
    assert guest["match"]["shots"] == 1 and guest["match"]["makes"] == 0
    assert sum(guest["match"]["misses"].values()) == 1
    assert guest["record"] == {"race": {"played": 1, "wins": 0}}


def test_solo_xp_syncs_feed_the_stats(client):
    token, _ = make_guest(client, "Solo")
    body = {"shots": 10, "makes": 6, "swishes": 2, "banks": 1, "best_streak": 3, "runs": 1, "misses": {"short": 3, "left": 1}}
    assert client.post("/xp", json=body, headers=auth(token)).status_code == 200
    assert client.post("/xp", json={"shots": 2, "makes": 0, "misses": {"short": 2}}, headers=auth(token)).status_code == 200

    stats = client.get("/me/stats", headers=auth(token)).json()
    assert stats["solo"] == {"shots": 12, "makes": 6, "swishes": 2, "banks": 1, "misses": {"short": 5, "left": 1}}
    assert stats["totals"] == stats["solo"]
    assert stats["by_day"][0]["solo"] == stats["solo"] and stats["by_day"][0]["match"] is None
    assert stats["record"] == {}


def test_misses_must_fit_the_missed_shots(client):
    token, _ = make_guest(client, "Solo")
    res = client.post("/xp", json={"shots": 3, "makes": 2, "misses": {"short": 2}}, headers=auth(token))
    assert res.status_code == 422
    res = client.post("/xp", json={"shots": 3, "makes": 0, "misses": {"sideways": 1}}, headers=auth(token))
    assert res.status_code == 422


def test_stats_window_and_paging(client, engine):
    token, uid = make_guest(client, "Old")
    with Session(engine) as s:
        s.add(PlayerDay(user_id=uuid.UUID(uid), day="20200101", source="solo", shots=50, makes=20))
        s.commit()
    assert client.get("/me/stats?days=7", headers=auth(token)).json()["totals"]["shots"] == 0
    assert client.get("/me/stats?days=0", headers=auth(token)).status_code == 422
    assert client.get("/me/matches?limit=51", headers=auth(token)).status_code == 422
    assert client.get("/me/matches").status_code == 401


def test_history_pages_with_before(client, lobby, clock, engine):  # noqa: F811
    first_room = lobby("race")
    play_race(client, first_room, clock)
    second_room = lobby("race")  # same two players, a new room
    play_race(client, second_room, clock)
    host = first_room["host"]

    both = client.get("/me/matches", headers=auth(host)).json()
    assert len(both) == 2 and both[0]["started_at"] > both[1]["started_at"]  # newest first
    page1 = client.get("/me/matches?limit=1", headers=auth(host)).json()
    assert [m["id"] for m in page1] == [both[0]["id"]]
    page2 = client.get("/me/matches", params={"limit": 1, "before": page1[0]["started_at"]}, headers=auth(host)).json()
    assert [m["id"] for m in page2] == [both[1]["id"]]
    assert client.get("/me/matches", params={"before": page2[0]["started_at"]}, headers=auth(host)).json() == []
    assert client.get("/me/stats", headers=auth(host)).json()["record"] == {"race": {"played": 2, "wins": 2}}


def test_erasing_yourself_removes_your_history(client, lobby, clock, engine):  # noqa: F811
    room = lobby("race")
    play_race(client, room, clock)
    assert client.delete("/auth/me", headers=auth(room["guest"])).status_code == 204
    with Session(engine) as s:
        assert s.exec(select(MatchPlayer).where(MatchPlayer.user_id == uuid.UUID(room["guest_id"]))).all() == []
        assert s.exec(select(PlayerDay).where(PlayerDay.user_id == uuid.UUID(room["guest_id"]))).all() == []
    # The opponent's history still has the match, now without them
    host = client.get("/me/matches", headers=auth(room["host"])).json()
    assert [p["nickname"] for p in host[0]["players"]] == ["Host"]
