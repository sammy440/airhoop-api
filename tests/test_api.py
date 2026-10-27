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


def score(client, token, mode="timed60", difficulty="starter", points=10, shots=20, makes=10):
    return client.post("/scores", json={"mode": mode, "difficulty": difficulty, "score": points, "shots": shots, "makes": makes}, headers=auth(token))


def test_leaderboard_best_per_player(client):
    a, _ = make_guest(client, "Alice")
    b, _ = make_guest(client, "Bola")
    for t, pts in [(a, 40), (a, 90), (b, 70), (b, 70)]:
        assert score(client, t, points=pts).status_code == 201
    score(client, b, mode="streak", points=12, makes=12)

    board = client.get("/leaderboard", params={"mode": "timed60"}).json()
    assert [(e["rank"], e["nickname"], e["best"], e["games"]) for e in board] == [(1, "Alice", 90, 2), (2, "Bola", 70, 2)]


def test_leaderboard_ties_share_rank_and_show_avatars(client):
    look = {"version": 1, "gender": "male", "skin": "#8a5530", "hair": "afro", "hairColor": "#1a120c",
            "jersey": "#13804f", "shorts": "#17110d", "shoes": "#f4f1ea", "number": 4}
    a = client.post("/auth/guest", json={"nickname": "Ace", "avatar": look}).json()["token"]
    b, _ = make_guest(client, "Bee")
    c, _ = make_guest(client, "Cee")
    for t, pts in [(a, 50), (b, 50), (c, 20)]:
        score(client, t, points=pts)
    board = client.get("/leaderboard", params={"mode": "timed60"}).json()
    assert [e["rank"] for e in board] == [1, 1, 3]
    assert board[0]["avatar"]["number"] == 4 and board[1]["avatar"] is None


def test_submit_returns_rank_and_personal_bests(client, engine):
    a, _ = make_guest(client, "Ada")
    b, b_id = make_guest(client, "Ben")
    score(client, a, points=80)
    saved = score(client, b, points=30).json()
    assert saved == {"status": "saved", "best": 30, "rank": 2}
    assert score(client, b, points=95, makes=15).json() == {"status": "saved", "best": 95, "rank": 1}

    # An older, higher score only counts for the all-time board.
    with Session(engine) as s:
        s.add(Score(user_id=b_id, mode="timed60", difficulty="starter", score=120, created_at=datetime.now(UTC) - timedelta(days=3)))
        s.commit()
    me = client.get("/leaderboard/me", params={"mode": "timed60", "difficulty": "starter"}, headers=auth(b)).json()
    assert me["day"] == {"best": 95, "games": 2, "rank": 1}
    assert me["week"]["best"] == 120 and me["all"]["games"] == 3
    assert client.get("/leaderboard/me", params={"mode": "streak"}, headers=auth(b)).json() == {"day": None, "week": None, "all": None}
    assert client.get("/leaderboard/me").status_code == 401


def test_leaderboard_period_and_difficulty_filters(client, engine):
    a, a_id = make_guest(client, "Ada")
    score(client, a, difficulty="legend", points=10)
    with Session(engine) as s:
        s.add(Score(user_id=a_id, mode="timed60", difficulty="legend", score=500, created_at=datetime.now(UTC) - timedelta(days=3)))
        s.commit()
    assert client.get("/leaderboard", params={"mode": "timed60"}).json()[0]["best"] == 500
    assert client.get("/leaderboard", params={"mode": "timed60", "period": "week"}).json()[0]["best"] == 500
    assert client.get("/leaderboard", params={"mode": "timed60", "period": "day"}).json()[0]["best"] == 10
    assert client.get("/leaderboard", params={"mode": "timed60", "difficulty": "rookie"}).json() == []


def test_score_validation(client):
    t, _ = make_guest(client, "Val")
    assert score(client, t, mode="timed").status_code == 422
    assert score(client, t, mode="free", points=-1).status_code == 422
    assert client.get("/leaderboard", params={"mode": "drop table"}).status_code == 422


def test_impossible_scores_are_rejected(client):
    t, _ = make_guest(client, "Cheat")
    assert score(client, t, points=9 * 10, makes=10, shots=10).status_code == 201  # perfect starter run
    for bad in [
        dict(makes=11, shots=10),  # more makes than shots
        dict(points=91, makes=10, shots=10),  # beyond 9 points per make on starter
        dict(mode="timed30", points=10, makes=10, shots=31),  # > 1 shot per second
        dict(mode="timed10", points=1, makes=1, shots=1),  # timed runs are 30 s+
        dict(mode="streak", points=11, makes=10, shots=10),  # streak longer than makes
    ]:
        assert score(client, t, **bad).status_code == 422, bad
    assert score(client, t, difficulty="legend", points=180, makes=10, shots=10).status_code == 201


def test_rate_limits(client):
    client.app.state.limiter.hits.clear()
    codes = [client.post("/auth/guest", json={"nickname": f"Spam{i}"}).status_code for i in range(12)]
    assert codes.count(201) == 10 and codes[-1] == 429
    res = client.post("/auth/guest", json={"nickname": "Again"})
    assert res.json()["detail"]["code"] == "rate_limited" and int(res.headers["retry-after"]) >= 1


def test_health_ready_and_client_errors(client):
    assert client.get("/health/ready").json() == {"status": "ok", "db": "ok", "rooms": 0}
    assert client.post("/telemetry/errors", json={"message": "TypeError: x is undefined", "url": "/play"}).status_code == 202
    assert client.post("/telemetry/errors", json={"message": "x" * 501}).status_code == 422


def test_delete_my_data(client, engine):
    t, uid = make_guest(client, "Gone")
    other, _ = make_guest(client, "Stays")
    score(client, t, points=50)
    score(client, other, points=40)
    assert client.delete("/auth/me", headers=auth(t)).status_code == 204
    assert client.get("/auth/me", headers=auth(t)).status_code == 401
    assert [e["nickname"] for e in client.get("/leaderboard", params={"mode": "timed60"}).json()] == ["Stays"]
    with Session(engine) as s:
        assert s.exec(select(Score).where(Score.user_id == uid)).all() == []


def test_cors_origins():
    from app.config import Settings

    s = Settings(allowed_origins=' "https://airhoops.vercel.app/" , https://https://play.example.com/,  ')
    assert s.origins == ["https://airhoops.vercel.app", "https://play.example.com"]


def test_cors_allows_vercel_deployments(client):
    def allowed(origin: str):
        res = client.options("/leaderboard", headers={"Origin": origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "content-type"})
        return res.headers.get("access-control-allow-origin") == origin

    assert allowed("http://localhost:3000")
    assert allowed("https://airhoops.vercel.app")
    assert allowed("https://airhoops-git-master-sammie440s-projects.vercel.app")
    assert allowed("https://airhoops-4f7k2xq1-sammie440s-projects.vercel.app")
    assert not allowed("https://airhoops-evil.vercel.app")
    assert not allowed("https://evil.example.com")
