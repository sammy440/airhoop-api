"""Invites: play again with someone you've played, without sharing a code."""

from app.rooms import INVITE_TTL_S

from .conftest import auth, make_guest
from .test_history import play_race
from .test_match_ws import lobby  # noqa: F401 - fixture


def new_room(client, token):
    return client.post("/rooms", json={"mode": "shootout"}, headers=auth(token)).json()["code"]


def test_invite_someone_you_played(client, lobby, clock):  # noqa: F811
    room = lobby("race")
    play_race(client, room, clock)
    code = new_room(client, room["host"])

    res = client.post("/invites", json={"to": room["guest_id"], "code": code}, headers=auth(room["host"]))
    assert res.status_code == 201, res.text
    invites = client.get("/invites", headers=auth(room["guest"])).json()
    assert len(invites) == 1
    assert invites[0] | {"age_s": 0} == {
        "from_id": room["host_id"], "nickname": "Host", "avatar": None, "code": code,
        "mode": "shootout", "difficulty": "starter", "race_seconds": 60, "best_of": 1, "players": 1, "age_s": 0,
    }

    # Taking it up uses it
    assert client.post(f"/rooms/{code}/join", headers=auth(room["guest"])).status_code == 200
    assert client.get("/invites", headers=auth(room["guest"])).json() == []


def test_only_people_you_have_played(client):
    a, _ = make_guest(client, "Ada")
    _, b_id = make_guest(client, "Bayo")
    code = new_room(client, a)
    res = client.post("/invites", json={"to": b_id, "code": code}, headers=auth(a))
    assert res.status_code == 403 and res.json()["detail"]["code"] == "not_an_opponent"


def test_only_to_your_own_open_room(client, lobby, clock):  # noqa: F811
    room = lobby("race")
    play_race(client, room, clock)
    other, _ = make_guest(client, "Other")
    theirs = new_room(client, other)
    res = client.post("/invites", json={"to": room["guest_id"], "code": theirs}, headers=auth(room["host"]))
    assert res.status_code == 403 and res.json()["detail"]["code"] == "not_in_room"
    assert client.post("/invites", json={"to": room["guest_id"], "code": "ZZZZZ"}, headers=auth(room["host"])).status_code == 404


def test_invites_go_stale(client, lobby, clock):  # noqa: F811
    room = lobby("race")
    play_race(client, room, clock)
    code = new_room(client, room["host"])
    client.post("/invites", json={"to": room["guest_id"], "code": code}, headers=auth(room["host"]))
    clock.advance(INVITE_TTL_S + 1)
    assert client.get("/invites", headers=auth(room["guest"])).json() == []


def test_dismiss_and_resend(client, lobby, clock):  # noqa: F811
    room = lobby("race")
    play_race(client, room, clock)
    code = new_room(client, room["host"])
    send = lambda: client.post("/invites", json={"to": room["guest_id"], "code": code}, headers=auth(room["host"]))  # noqa: E731
    send()
    send()  # one per sender
    assert len(client.get("/invites", headers=auth(room["guest"])).json()) == 1
    assert client.delete(f"/invites/{room['host_id']}", headers=auth(room["guest"])).status_code == 204
    assert client.get("/invites", headers=auth(room["guest"])).json() == []


def test_a_room_that_left_the_lobby_drops_its_invites(client, lobby, clock):  # noqa: F811
    room = lobby("race")
    play_race(client, room, clock)
    code = new_room(client, room["host"])
    client.post("/invites", json={"to": room["guest_id"], "code": code}, headers=auth(room["host"]))
    client.app.state.rooms.rooms[code].phase = "in_progress"
    assert client.get("/invites", headers=auth(room["guest"])).json() == []
