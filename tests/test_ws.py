"""WebSocket rooms: presence, lifecycle, reconnects, heartbeats, cleanup (README Week 6 'done when')."""

import pytest
from sqlmodel import Session, select
from starlette.websockets import WebSocketDisconnect

from app.models import Match

from .conftest import auth, make_guest, receive_until


@pytest.fixture
def room(client):
    """A room with host + guest seats (not yet connected)."""
    host, host_id = make_guest(client, "Host")
    guest, guest_id = make_guest(client, "Guest")
    code = client.post("/rooms", json={"mode": "timed"}, headers=auth(host)).json()["code"]
    client.post(f"/rooms/{code}/join", headers=auth(guest))
    return {"code": code, "host": host, "host_id": host_id, "guest": guest, "guest_id": guest_id}


def connect(client, room, who):
    return client.websocket_connect(f"/ws/{room['code']}?token={room[who]}")


def players(state):
    return {p["nickname"]: p for p in state["room"]["players"]}


def test_two_tabs_see_each_others_presence(client, room):
    with connect(client, room, "host") as a:
        first = receive_until(a, "room_state")
        assert first["you"] == room["host_id"]
        assert players(first)["Host"]["connected"] and not players(first)["Guest"]["connected"]

        with connect(client, room, "guest") as b:
            joined = receive_until(a, "presence")
            assert joined["event"] == "joined" and joined["player"]["nickname"] == "Guest"
            state_a = receive_until(a, "room_state")
            state_b = receive_until(b, "room_state")
            for state in (state_a, state_b):
                assert players(state)["Host"]["connected"] and players(state)["Guest"]["connected"]
            assert state_b["you"] == room["guest_id"]


def test_rejects_bad_token_and_unknown_room(client, room):
    with client.websocket_connect(f"/ws/{room['code']}?token=bad") as ws:
        assert ws.receive_json()["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401
    with client.websocket_connect(f"/ws/NOPE1?token={room['host']}") as ws:
        assert ws.receive_json()["code"] == "room_not_found"


def test_ping_pong_and_unknown_messages(client, room):
    with connect(client, room, "host") as a:
        receive_until(a, "room_state")
        a.send_json({"type": "ping", "t": 123})
        assert receive_until(a, "pong")["t"] == 123
        a.send_json({"type": "dance"})
        assert receive_until(a, "error")["code"] == "unknown_message"
        a.send_text("not json")
        assert receive_until(a, "error")["code"] == "bad_json"


def test_lifecycle_lobby_ready_in_progress_finished(client, room, engine):
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        receive_until(a, "room_state")
        receive_until(b, "room_state")

        a.send_json({"type": "start"})
        assert receive_until(a, "error")["code"] == "not_ready"

        a.send_json({"type": "ready", "ready": True})
        assert receive_until(a, "room_state")["room"]["phase"] == "lobby"  # guest not ready yet
        b.send_json({"type": "ready", "ready": True})
        state = receive_until(b, "room_state")
        while state["room"]["phase"] != "ready":
            state = receive_until(b, "room_state")

        b.send_json({"type": "start"})
        assert receive_until(b, "error")["code"] == "not_host"

        a.send_json({"type": "start"})
        state = receive_until(a, "room_state")
        while state["room"]["phase"] != "in_progress":
            state = receive_until(a, "room_state")
        with Session(engine) as s:
            match = s.exec(select(Match)).one()
            assert match.room_code == room["code"] and match.ended_at is None

        a.send_json({"type": "end"})
        state = receive_until(a, "room_state")
        while state["room"]["phase"] != "finished":
            state = receive_until(a, "room_state")
        with Session(engine) as s:
            assert s.exec(select(Match)).one().ended_at is not None

        a.send_json({"type": "rematch"})
        state = receive_until(a, "room_state")
        while state["room"]["phase"] != "lobby":
            state = receive_until(a, "room_state")
        assert not any(p["ready"] for p in state["room"]["players"])


def test_unready_or_disconnect_drops_back_to_lobby(client, room):
    with connect(client, room, "host") as a:
        receive_until(a, "room_state")
        with connect(client, room, "guest") as b:
            receive_until(b, "room_state")
            a.send_json({"type": "ready"})
            b.send_json({"type": "ready"})
            state = receive_until(a, "room_state")
            while state["room"]["phase"] != "ready":
                state = receive_until(a, "room_state")
        # Guest's tab closed → no longer "everyone connected and ready".
        receive_until(a, "presence", event="disconnected")
        assert receive_until(a, "room_state")["room"]["phase"] == "lobby"


def test_room_survives_brief_disconnect(client, room, clock, manager):
    with connect(client, room, "host") as a:
        receive_until(a, "room_state")
        with connect(client, room, "guest") as b:
            receive_until(b, "room_state")
        receive_until(a, "presence", event="disconnected")
        state = receive_until(a, "room_state")
        assert not players(state)["Guest"]["connected"]

        clock.advance(20)  # within the 30 s grace
        client.portal.call(manager.sweep)

        with connect(client, room, "guest") as b2:
            receive_until(a, "presence", event="reconnected")
            state = receive_until(b2, "room_state")
            assert players(state)["Guest"]["connected"]
            assert len(state["room"]["players"]) == 2


def test_seat_expires_after_grace_and_host_moves_on(client, room, clock, manager):
    with connect(client, room, "guest") as b:
        receive_until(b, "room_state")
        with connect(client, room, "host") as a:
            receive_until(a, "room_state")
        receive_until(b, "presence", event="disconnected")

        # The guest keeps pinging (like the real client); the host's seat lapses.
        clock.advance(20)
        b.send_json({"type": "ping"})
        receive_until(b, "pong")
        clock.advance(11)
        client.portal.call(manager.sweep)
        left = receive_until(b, "presence", event="left")
        assert left["player"]["nickname"] == "Host"
        state = receive_until(b, "room_state")
        assert [p["nickname"] for p in state["room"]["players"]] == ["Guest"]
        assert state["room"]["host_id"] == room["guest_id"]


def test_empty_room_is_cleaned_up(client, room, clock, manager):
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        receive_until(a, "room_state")
        receive_until(b, "room_state")
    clock.advance(31)
    client.portal.call(manager.sweep)
    assert room["code"] not in manager.rooms
    assert client.get(f"/rooms/{room['code']}").status_code == 404


def test_room_nobody_ever_joined_expires(client, clock, manager):
    token, _ = make_guest(client, "Ghost")
    code = client.post("/rooms", json={}, headers=auth(token)).json()["code"]
    clock.advance(31)
    client.portal.call(manager.sweep)
    assert code not in manager.rooms


def test_silent_socket_is_dropped_by_heartbeat(client, room, clock, manager):
    with connect(client, room, "host") as a:
        receive_until(a, "room_state")
        clock.advance(26)  # no ping for longer than the 25 s heartbeat timeout
        client.portal.call(manager.sweep)
        with pytest.raises(WebSocketDisconnect) as e:
            for _ in range(5):
                a.receive_json()
        assert e.value.code == 4000
    assert not manager.rooms[room["code"]].players[room["host_id"]].connected


def test_pings_keep_a_socket_alive(client, room, clock, manager):
    with connect(client, room, "host") as a:
        receive_until(a, "room_state")
        for _ in range(3):
            clock.advance(20)
            a.send_json({"type": "ping"})
            receive_until(a, "pong")
            client.portal.call(manager.sweep)
        assert manager.rooms[room["code"]].players[room["host_id"]].connected


def test_second_tab_replaces_first(client, room):
    with connect(client, room, "host") as first:
        receive_until(first, "room_state")
        with connect(client, room, "host") as second:
            receive_until(second, "room_state")
            with pytest.raises(WebSocketDisconnect) as e:
                for _ in range(5):
                    first.receive_json()
            assert e.value.code == 4001


def test_leave_frees_the_seat(client, room, manager):
    with connect(client, room, "host") as a:
        receive_until(a, "room_state")
        with connect(client, room, "guest") as b:
            receive_until(b, "room_state")
            b.send_json({"type": "leave"})
            assert receive_until(a, "presence", event="left")["player"]["nickname"] == "Guest"
    assert list(manager.rooms[room["code"]].players) == [room["host_id"]]
