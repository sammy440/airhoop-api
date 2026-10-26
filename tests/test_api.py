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
    assert me.json() == {"id": user_id, "nickname": "Dayo"}
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


# ── Rooms ──


def test_create_get_and_join_room(client):
    host, host_id = make_guest(client, "Host")
    guest, guest_id = make_guest(client, "Guest")
    room = client.post("/rooms", json={"mode": "timed", "difficulty": "allstar"}, headers=auth(host)).json()
    code = room["code"]
    assert len(code) == 5 and code.isupper()
    assert room["phase"] == "lobby" and room["host_id"] == host_id
    assert room["mode"] == "timed" and room["difficulty"] == "allstar"

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
