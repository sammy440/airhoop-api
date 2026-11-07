"""Spectators: /ws/{code}?watch=1 sees the room and the match, takes no seat, can't act."""

import pytest
from starlette.websockets import WebSocketDisconnect

from app.match import COUNTDOWN_S
from app.rooms import MAX_SPECTATORS

from .conftest import auth, make_guest, receive_until
from .test_match import MAKE
from .test_match_ws import connect, lobby, start_match  # noqa: F401 - lobby is a fixture


def state_with(ws, watchers: int, limit: int = 10):
    """The next room_state showing this many watchers."""
    for _ in range(limit):
        msg = receive_until(ws, "room_state")
        if msg["room"]["spectators"] == watchers:
            return msg
    raise AssertionError(f"no room_state with {watchers} watchers")


def watch(client, code, token):
    return client.websocket_connect(f"/ws/{code}?token={token}&watch=1")


def test_a_watcher_sees_the_match_and_takes_no_seat(client, lobby, clock):  # noqa: F811
    room = lobby("race")
    fan, _ = make_guest(client, "Fan")
    with connect(client, room, "host") as a, connect(client, room, "guest") as b, watch(client, room["code"], fan) as w:
        state = receive_until(w, "room_state")
        assert state["you"] is None and state["spectating"] is True
        assert len(state["room"]["players"]) == 2 and state["room"]["spectators"] == 1

        start_match(a, b)
        assert receive_until(w, "match_start", limit=15)["mode"] == "race"
        clock.advance(COUNTDOWN_S + 0.5)
        a.send_json({"type": "throw", **MAKE, "t": clock.t})
        assert receive_until(w, "shot_result", limit=15)["scored"] is True

        # Reactions and chat reach them; they can't throw, react or chat
        b.send_json({"type": "react", "reaction": "fire"})
        assert receive_until(w, "reaction", limit=15)["reaction"] == "fire"
        for msg in ({"type": "throw", **MAKE}, {"type": "react", "reaction": "fire"}, {"type": "chat", "text": "hi"}, {"type": "start"}):
            w.send_json(msg)
            assert receive_until(w, "error", limit=15)["code"] == "spectating"
        w.send_json({"type": "ping", "t": 1})
        assert receive_until(w, "pong", limit=15)["t"] == 1


def test_joining_mid_match_replays_it(client, lobby, clock):  # noqa: F811
    room = lobby("race")
    fan, _ = make_guest(client, "Fan")
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_match(a, b)
        clock.advance(COUNTDOWN_S + 0.5)
        a.send_json({"type": "throw", **MAKE, "t": clock.t})
        receive_until(a, "shot_result")
        with watch(client, room["code"], fan) as w:
            receive_until(w, "room_state")
            assert receive_until(w, "match_start", limit=15)["mode"] == "race"
            assert receive_until(w, "scoreboard", limit=15)["scores"][room["host_id"]]["points"] == 3


def test_leaving_and_limits(client, lobby):  # noqa: F811
    room = lobby("race")
    fan, _ = make_guest(client, "Fan")
    with connect(client, room, "host") as a:
        with watch(client, room["code"], fan) as w:
            receive_until(w, "room_state")
            w.send_json({"type": "leave"})
        state_with(a, 0)
        assert client.get(f"/rooms/{room['code']}").json()["spectators"] == 0

        # A player can't also watch their own room
        with watch(client, room["code"], room["host"]) as w:
            assert receive_until(w, "error")["code"] == "already_in_room"

        # Room full of watchers
        client.app.state.rooms.rooms[room["code"]].spectators.update({f"x{i}": object() for i in range(MAX_SPECTATORS)})  # type: ignore[misc]
        with watch(client, room["code"], fan) as w:
            assert receive_until(w, "error")["code"] == "watchers_full"
        client.app.state.rooms.rooms[room["code"]].spectators.clear()


def test_quick_matches_and_missing_rooms_cant_be_watched(client):
    from .test_quickmatch import poll

    a, _ = make_guest(client, "Ada")
    b, _ = make_guest(client, "Bayo")
    fan, _ = make_guest(client, "Fan")
    poll(client, a)
    code = poll(client, b)["code"]
    with watch(client, code, fan) as w:
        assert receive_until(w, "error")["code"] == "not_watchable"
    with watch(client, "ZZZZZ", fan) as w:
        assert receive_until(w, "error")["code"] == "room_not_found"
        with pytest.raises(WebSocketDisconnect):
            w.receive_json()


def test_room_state_counts_watchers(client):
    host, _ = make_guest(client, "Host")
    code = client.post("/rooms", json={"mode": "shootout"}, headers=auth(host)).json()["code"]
    assert client.get(f"/rooms/{code}").json()["spectators"] == 0
