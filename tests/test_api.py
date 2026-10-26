from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.auth import hash_token
from app.models import Score, User

from .conftest import auth, make_guest


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


# ── Guest auth ──


def test_guest_session_and_me(client, engine):
    token, user_id = make_guest(client, "Dayo")
    me = client.get("/auth/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json() == {"id": user_id, "nickname": "Dayo", "avatar": None}
    with Session(engine) as s:
        user = s.exec(select(User)).one()
        assert user.token_hash == hash_token(token)
        assert token not in user.token_hash  # only the hash is stored


def test_auth_rejects_missing_or_bad_tokens(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers=auth("nope")).status_code == 401


def test_nickname_validation(client):
    for bad in ["", "a", "x" * 21, "<script>", "   "]:
        assert client.post("/auth/guest", json={"nickname": bad}).status_code == 422, bad
    assert client.post("/auth/guest", json={"nickname": "  Ade_23  "}).json()["user"]["nickname"] == "Ade_23"


def test_rename(client):
    token, _ = make_guest(client, "Old")
    assert client.patch("/auth/me", json={"nickname": "New Name"}, headers=auth(token)).json()["nickname"] == "New Name"


AVATAR = {
    "version": 1, "gender": "female", "skin": "#4A2A17", "hair": "braids", "hairColor": "#1a120c",
    "jersey": "#1f4fd1", "shorts": "#f4f1ea", "shoes": "#17110d", "number": 7,
}


def test_avatar_on_signup_update_and_rooms(client):
    token = client.post("/auth/guest", json={"nickname": "Styled", "avatar": AVATAR}).json()["token"]
    me = client.get("/auth/me", headers=auth(token)).json()
    assert me["avatar"]["skin"] == "#4a2a17" and me["avatar"]["hair"] == "braids"  # hex normalised

    plain, _ = make_guest(client, "Plain")
    assert client.get("/auth/me", headers=auth(plain)).json()["avatar"] is None

    # Name and look can change independently.
    out = client.patch("/auth/me", json={"avatar": {**AVATAR, "number": 99}}, headers=auth(plain)).json()
    assert out["nickname"] == "Plain" and out["avatar"]["number"] == 99
    out = client.patch("/auth/me", json={"nickname": "Renamed"}, headers=auth(plain)).json()
    assert out["nickname"] == "Renamed" and out["avatar"]["number"] == 99

    code = client.post("/rooms", json={"mode": "race"}, headers=auth(token)).json()["code"]
    room = client.post(f"/rooms/{code}/join", headers=auth(plain)).json()
    assert [p["avatar"]["number"] for p in room["players"]] == [7, 99]

    client.patch("/auth/me", json={"avatar": {**AVATAR, "number": 1}}, headers=auth(plain))
    assert client.get(f"/rooms/{code}").json()["players"][1]["avatar"]["number"] == 1


def test_avatar_validation(client):
    token, _ = make_guest(client, "Strict")
    for bad in [
        {**AVATAR, "skin": "red"},
        {**AVATAR, "skin": "#12345"},
        {**AVATAR, "gender": "robot"},
        {**AVATAR, "hair": "mohawk"},
        {**AVATAR, "number": 100},
        {**AVATAR, "script": "<x>"},
    ]:
        assert client.patch("/auth/me", json={"avatar": bad}, headers=auth(token)).status_code == 422, bad


# ── Rooms ──


def test_create_get_and_join_room(client):
    host, host_id = make_guest(client, "Host")
    guest, guest_id = make_guest(client, "Guest")
    room = client.post("/rooms", json={"mode": "race", "difficulty": "allstar"}, headers=auth(host)).json()
    code = room["code"]
    assert len(code) == 5 and code.isupper()
    assert room["phase"] == "lobby" and room["host_id"] == host_id
    assert room["mode"] == "race" and room["difficulty"] == "allstar"

    joined = client.post(f"/rooms/{code.lower()}/join", headers=auth(guest)).json()  # codes are case-insensitive
    assert [p["id"] for p in joined["players"]] == [host_id, guest_id]
    assert client.get(f"/rooms/{code}").json()["code"] == code


def test_room_errors(client):
    token, _ = make_guest(client, "Solo")
    assert client.post("/rooms", headers=auth(token), json={}).status_code == 201
    assert client.post("/rooms", json={}).status_code == 401
    missing = client.post("/rooms/ZZZZZ/join", headers=auth(token))
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "room_not_found"
    assert client.post("/rooms", headers=auth(token), json={"mode": "nonsense"}).status_code == 422


def test_room_capacity(client):
    tokens = [make_guest(client, f"P{i}")[0] for i in range(5)]
    code = client.post("/rooms", json={}, headers=auth(tokens[0])).json()["code"]
    for t in tokens[1:4]:
        assert client.post(f"/rooms/{code}/join", headers=auth(t)).status_code == 200
    full = client.post(f"/rooms/{code}/join", headers=auth(tokens[4]))
    assert full.status_code == 409 and full.json()["detail"]["code"] == "room_full"
    # Rejoining your own seat is fine even when full.
    assert client.post(f"/rooms/{code}/join", headers=auth(tokens[1])).status_code == 200


# ── Scores / leaderboard ──


def test_leaderboard_best_per_player(client):
    a, _ = make_guest(client, "Alice")
    b, _ = make_guest(client, "Bola")
    for t, score in [(a, 40), (a, 90), (b, 70), (b, 70)]:
        assert client.post("/scores", json={"mode": "timed60", "difficulty": "starter", "score": score}, headers=auth(t)).status_code == 201
    client.post("/scores", json={"mode": "streak", "difficulty": "starter", "score": 999}, headers=auth(b))

    board = client.get("/leaderboard", params={"mode": "timed60"}).json()
    assert [(e["rank"], e["nickname"], e["best"], e["games"]) for e in board] == [(1, "Alice", 90, 2), (2, "Bola", 70, 2)]


def test_leaderboard_period_and_difficulty_filters(client, engine):
    a, a_id = make_guest(client, "Ada")
    client.post("/scores", json={"mode": "timed60", "difficulty": "legend", "score": 10}, headers=auth(a))
    with Session(engine) as s:
        s.add(Score(user_id=a_id, mode="timed60", difficulty="legend", score=500, created_at=datetime.now(UTC) - timedelta(days=3)))
        s.commit()
    assert client.get("/leaderboard", params={"mode": "timed60"}).json()[0]["best"] == 500
    assert client.get("/leaderboard", params={"mode": "timed60", "period": "day"}).json()[0]["best"] == 10
    assert client.get("/leaderboard", params={"mode": "timed60", "difficulty": "rookie"}).json() == []


def test_score_validation(client):
    t, _ = make_guest(client, "Val")
    assert client.post("/scores", json={"mode": "timed", "difficulty": "starter", "score": 5}, headers=auth(t)).status_code == 422
    assert client.post("/scores", json={"mode": "free", "difficulty": "starter", "score": -1}, headers=auth(t)).status_code == 422
    assert client.get("/leaderboard", params={"mode": "drop table"}).status_code == 422
