"""Personal bests and ghosts kept with the account: /me/bests."""

from datetime import timedelta

from app.game import daily

from .conftest import auth, make_guest

AT = "2026-11-06T10:00:00Z"


def ghost(points: list[int]) -> list[dict]:
    return [{"t": 2.0 + i * 2, "speed": 7.2, "angle": 52.0, "aim": 0.0, "wind": 0.3, "phase": 1.1 + i, "points": p} for i, p in enumerate(points)]


def run(points: list[int], **extra) -> dict:
    makes = sum(1 for p in points if p)
    return {"points": sum(points), "makes": makes, "streak": min(makes, 2), "at": AT, "ghost": ghost(points), **extra}


def test_a_best_and_its_ghost_follow_the_player(client):
    token, _ = make_guest(client, "Ayo")
    res = client.put("/me/bests/timed60:rookie", json=run([3, 0, 4, 3]), headers=auth(token))
    assert res.status_code == 200, res.text
    assert res.json() == {"points": 10, "makes": 3, "streak": 2, "at": "2026-11-06T10:00:00Z", "has_ghost": True, "updated": True}

    assert client.get("/me/bests", headers=auth(token)).json() == {
        "timed60:rookie": {"points": 10, "makes": 3, "streak": 2, "at": "2026-11-06T10:00:00Z", "has_ghost": True}
    }
    shots = client.get("/me/bests/timed60:rookie/ghost", headers=auth(token)).json()
    assert [s["points"] for s in shots] == [3, 0, 4, 3] and shots[0]["speed"] == 7.2

    # Private
    other, _ = make_guest(client, "Other")
    assert client.get("/me/bests", headers=auth(other)).json() == {}
    assert client.get("/me/bests/timed60:rookie/ghost", headers=auth(other)).status_code == 404


def test_only_a_better_run_replaces_the_best(client):
    token, _ = make_guest(client, "Ayo")
    client.put("/me/bests/timed60:rookie", json=run([3, 3, 3]), headers=auth(token))
    worse = client.put("/me/bests/timed60:rookie", json=run([3, 3]), headers=auth(token)).json()
    assert worse["updated"] is False and worse["points"] == 9
    better = client.put("/me/bests/timed60:rookie", json=run([4, 3, 3]), headers=auth(token)).json()
    assert better["updated"] is True and better["points"] == 10
    assert [s["points"] for s in client.get("/me/bests/timed60:rookie/ghost", headers=auth(token)).json()] == [4, 3, 3]


def test_streak_boards_are_won_on_the_streak(client):
    token, _ = make_guest(client, "Ayo")
    client.put("/me/bests/streak:starter", json={"points": 40, "makes": 9, "streak": 9, "at": AT}, headers=auth(token))
    res = client.put("/me/bests/streak:starter", json={"points": 60, "makes": 12, "streak": 8, "at": AT}, headers=auth(token)).json()
    assert res["updated"] is False and res["streak"] == 9 and res["has_ghost"] is False


def test_impossible_bests_are_refused(client):
    token, _ = make_guest(client, "Ayo")
    put = lambda key, body: client.put(f"/me/bests/{key}", json=body, headers=auth(token))  # noqa: E731
    assert put("timed60:rookie", {"points": 500, "makes": 3, "streak": 1, "at": AT}).status_code == 422  # too many points
    assert put("timed60:rookie", {"points": 9, "makes": 3, "streak": 4, "at": AT}).status_code == 422  # streak > makes
    assert put("timed60:rookie", run([3, 3]) | {"points": 9}).status_code == 422  # ghost doesn't add up
    assert put("streak:rookie", run([3, 3])).status_code == 422  # no clock, no ghost
    assert put("timed30:rookie", run([3] * 31)).status_code == 422  # more shots than seconds
    assert put("timed99:rookie", run([3])).status_code == 200
    assert put("timed10:rookie", run([3])).status_code == 422  # not a real clock
    assert put("timed60:pro", run([3])).status_code == 422  # not a real difficulty
    assert put("daily20261399", run([3])).status_code == 422  # not a real day
    late = run([3]) | {"ghost": [{**ghost([3])[0], "t": 75}]}
    assert put("timed60:rookie", late).status_code == 422  # after the buzzer


def test_old_dailies_drop_off_the_list(client):
    token, _ = make_guest(client, "Ayo")
    today = daily.today()
    for back in (0, 5):
        key = f"daily{(today - timedelta(days=back)):%Y%m%d}"
        rules = daily.daily_rules(today - timedelta(days=back))
        assert rules.seconds >= 2
        res = client.put(f"/me/bests/{key}", json=run([3]), headers=auth(token))
        assert res.status_code == 200, res.text
    assert list(client.get("/me/bests", headers=auth(token)).json()) == [f"daily{today:%Y%m%d}"]


def test_erasing_yourself_removes_your_bests(client):
    token, _ = make_guest(client, "Ayo")
    client.put("/me/bests/timed60:rookie", json=run([3]), headers=auth(token))
    assert client.delete("/auth/me", headers=auth(token)).status_code == 204
    fresh, _ = make_guest(client, "Ayo")
    assert client.get("/me/bests", headers=auth(fresh)).json() == {}


def test_needs_a_player(client):
    assert client.get("/me/bests").status_code == 401
    assert client.put("/me/bests/timed60:rookie", json=run([3])).status_code == 401
