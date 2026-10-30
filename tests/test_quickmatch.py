"""Quick match: the queue pairs strangers, and their race starts by itself once both connect."""

import pytest

from .conftest import auth, make_guest, receive_until


@pytest.fixture
def players(client):
    return [make_guest(client, name) for name in ("Ada", "Bayo", "Chi")]


def poll(client, token, difficulty="any"):
    res = client.post("/quickmatch", json={"difficulty": difficulty}, headers=auth(token))
    assert res.status_code == 200, res.text
    return res.json()


def test_two_players_are_paired_into_one_room(client, players):
    (a, a_id), (b, b_id), _ = players
    first = poll(client, a)
    assert first["status"] == "waiting" and first["waiting"] == 1
    matched = poll(client, b)
    assert matched["status"] == "matched"
    # The first player gets the same room on their next poll.
    assert poll(client, a) == {"status": "matched", "code": matched["code"], "waiting": None, "waited_s": None}

    room = client.get(f"/rooms/{matched['code']}").json()
    assert room["quick"] is True and room["mode"] == "race" and room["race_seconds"] == 60
    assert room["host_id"] == a_id  # the longer wait hosts
    assert {p["id"] for p in room["players"]} == {a_id, b_id}
    assert all(p["ready"] for p in room["players"])


def test_the_race_starts_once_both_connect(client, players):
    (a, _), (b, _), _ = players
    poll(client, a)
    code = poll(client, b)["code"]
    with client.websocket_connect(f"/ws/{code}?token={a}") as wa:
        receive_until(wa, "room_state")
        with client.websocket_connect(f"/ws/{code}?token={b}") as wb:
            for ws in (wa, wb):
                start = receive_until(ws, "match_start", limit=15)
                assert start["mode"] == "race"


def test_unreadying_holds_the_start_until_ready_again(client, players):
    (a, _), (b, _), _ = players
    poll(client, a)
    code = poll(client, b)["code"]
    with client.websocket_connect(f"/ws/{code}?token={a}") as wa:
        receive_until(wa, "room_state")
        wa.send_json({"type": "ready", "ready": False})
        receive_until(wa, "room_state")
        with client.websocket_connect(f"/ws/{code}?token={b}") as wb:
            assert receive_until(wb, "room_state")["room"]["phase"] == "lobby"
            wa.send_json({"type": "ready", "ready": True})
            for ws in (wa, wb):
                receive_until(ws, "match_start", limit=15)


def test_difficulty_must_agree_unless_either_says_any(client, players):
    (a, _), (b, _), (c, _) = players
    assert poll(client, a, "legend")["status"] == "waiting"
    assert poll(client, b, "starter")["status"] == "waiting"
    matched = poll(client, c, "any")
    assert matched["status"] == "matched"
    assert client.get(f"/rooms/{matched['code']}").json()["difficulty"] == "legend"  # longest wait first


def test_two_anys_play_starter(client, players):
    (a, _), (b, _), _ = players
    poll(client, a)
    code = poll(client, b)["code"]
    assert client.get(f"/rooms/{code}").json()["difficulty"] == "starter"


def test_players_who_stop_polling_or_cancel_drop_out(client, players, clock):
    (a, _), (b, _), (c, _) = players
    poll(client, a)
    clock.advance(11)  # a closed the tab
    assert poll(client, b)["status"] == "waiting"
    assert client.delete("/quickmatch", headers=auth(b)).status_code == 204
    assert poll(client, c)["status"] == "waiting"


def test_needs_a_session(client):
    assert client.post("/quickmatch", json={}).status_code == 401
    assert client.post("/quickmatch", json={"difficulty": "impossible"}, headers=auth("nope")).status_code in (401, 422)
