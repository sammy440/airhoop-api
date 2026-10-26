"""Week 7 'done when', end-to-end over WebSockets:
results match on both screens, impossible throws are rejected, dropped players are handled."""

import uuid

import pytest
from sqlmodel import Session, select

from app.match import COUNTDOWN_S
from app.models import Match, Score, Throw

from .conftest import auth, make_guest, receive_until
from .test_match import MAKE, MISS


@pytest.fixture
def lobby(client):
    host, host_id = make_guest(client, "Host")
    guest, guest_id = make_guest(client, "Guest")

    def make(mode="race", **extra):
        code = client.post("/rooms", json={"mode": mode, "race_seconds": 30, **extra}, headers=auth(host)).json()["code"]
        client.post(f"/rooms/{code}/join", headers=auth(guest))
        return {"code": code, "host": host, "host_id": host_id, "guest": guest, "guest_id": guest_id}

    return make


def connect(client, room, who):
    return client.websocket_connect(f"/ws/{room['code']}?token={room[who]}")


def start_match(a, b):
    for ws in (a, b):
        receive_until(ws, "room_state")
    a.send_json({"type": "ready"})
    b.send_json({"type": "ready"})
    receive_until(a, "room_state", limit=10)
    a.send_json({"type": "start"})
    starts = [receive_until(ws, "match_start", limit=15) for ws in (a, b)]
    return starts


def test_both_screens_get_the_same_server_result(client, lobby, clock, engine):
    room = lobby("race")
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_a, start_b = start_match(a, b)
        assert start_a["mode"] == "race" and start_a["raceSeconds"] == 30
        assert start_a["startAt"] == start_b["startAt"] and "serverTime" in start_a

        a.send_json({"type": "throw", **MAKE})
        assert receive_until(a, "error")["code"] == "not_started"  # countdown still running

        clock.advance(COUNTDOWN_S + 0.5)
        a.send_json({"type": "throw", **MAKE, "t": clock.t})
        shot_a = receive_until(a, "shot_result")
        shot_b = receive_until(b, "shot_result")
        assert shot_a == shot_b  # identical authoritative result on both screens
        assert shot_a["playerId"] == room["host_id"] and shot_a["scored"] and shot_a["points"] == 3
        board = receive_until(b, "scoreboard")
        assert board["scores"][room["host_id"]]["points"] == 3

        # Impossible throw: rejected, only the thrower hears about it, nothing recorded.
        clock.advance(5)
        b.send_json({"type": "throw", "power": 50, "angle": 30, "arc": 0.7})
        assert receive_until(b, "error")["code"] == "invalid_throw"

        clock.advance(30)
        client.portal.call(client.app.state.rooms.tick_matches)
        end_a = receive_until(a, "match_end")
        end_b = receive_until(b, "match_end")
        assert end_a == end_b and end_a["winner"] == room["host_id"] and end_a["reason"] == "time"
        assert receive_until(a, "room_state")["room"]["phase"] == "finished"

    with Session(engine) as s:
        match = s.exec(select(Match)).one()
        assert match.winner_id == uuid.UUID(room["host_id"]) and match.ended_at is not None
        throws = s.exec(select(Throw)).all()
        assert len(throws) == 1 and throws[0].scored and throws[0].points == 3
        scores = {str(sc.user_id): sc.score for sc in s.exec(select(Score)).all()}
        assert scores == {room["host_id"]: 3, room["guest_id"]: 0}
        assert {sc.mode for sc in s.exec(select(Score)).all()} == {"race30"}


def test_rate_limit_and_turn_order(client, lobby, clock):
    room = lobby("shootout")
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_match(a, b)
        assert receive_until(a, "turn")["playerId"] == room["host_id"]
        clock.advance(COUNTDOWN_S + 0.5)
        b.send_json({"type": "throw", **MAKE})
        assert receive_until(b, "error")["code"] == "not_your_turn"
        a.send_json({"type": "throw", **MISS})
        receive_until(a, "shot_result")
        a.send_json({"type": "throw", **MAKE})
        assert receive_until(a, "error")["code"] == "not_your_turn"
        assert receive_until(b, "turn")["playerId"] == room["guest_id"]


def test_dropped_player_forfeits_after_the_grace_period(client, lobby, clock, manager):
    room = lobby("race")
    with connect(client, room, "host") as a:
        with connect(client, room, "guest") as b:
            start_match(a, b)
        receive_until(a, "presence", event="disconnected")
        clock.advance(20)
        a.send_json({"type": "ping"})
        receive_until(a, "pong")
        clock.advance(11)  # guest's 30 s seat window has passed
        client.portal.call(manager.sweep)
        assert receive_until(a, "forfeit")["playerId"] == room["guest_id"]
        end = receive_until(a, "match_end")
        assert end["winner"] == room["host_id"] and end["reason"] == "forfeit"


def test_reconnecting_mid_match_restores_the_match_view(client, lobby, clock):
    room = lobby("shootout")
    with connect(client, room, "host") as a:
        with connect(client, room, "guest") as b:
            start_match(a, b)
        clock.advance(5)
        with connect(client, room, "guest") as b2:
            start = receive_until(b2, "match_start")
            assert start["mode"] == "shootout"
            receive_until(b2, "scoreboard")
            assert receive_until(b2, "turn")["playerId"] == room["host_id"]


def test_leaving_mid_match_forfeits(client, lobby):
    room = lobby("race")
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_match(a, b)
        b.send_json({"type": "leave"})
        assert receive_until(a, "match_end", limit=15)["winner"] == room["host_id"]


def test_host_can_abort(client, lobby):
    room = lobby("race")
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_match(a, b)
        a.send_json({"type": "end"})
        end = receive_until(b, "match_end", limit=15)
        assert end["reason"] == "aborted" and end["winners"] == []
