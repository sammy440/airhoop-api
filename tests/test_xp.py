"""XP and levels: the rules (same examples as the frontend's xp.test.ts) and the API."""

from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from app.game.xp import MAX_LEVEL, XpStats, level_for, xp_for, xp_to_reach
from app.models import User
from app.routes import SOLO_XP_DAY_CAP

from .conftest import auth, make_guest


def test_xp_for_play():
    # 20 shots, 12 makes (3 swishes, 1 bank), best streak 5, one finished run
    assert xp_for(XpStats(shots=20, makes=12, swishes=3, banks=1, best_streak=5, runs=1)) == 20 + 120 + 15 + 5 + 15 + 25
    assert xp_for(XpStats(matches=1, wins=1)) == 70
    assert xp_for(XpStats(best_streak=2)) == 0


def test_level_curve():
    assert [xp_to_reach(n) for n in (1, 2, 3, 4, 5, 10)] == [0, 100, 250, 450, 700, 2700]
    assert level_for(0) == 1 and level_for(99) == 1 and level_for(100) == 2 and level_for(249) == 2 and level_for(250) == 3
    assert level_for(10**9) == MAX_LEVEL


def test_gain_xp_counts_play_not_claims(client):
    token, _ = make_guest(client, "Xp")
    r = client.post("/xp", json={"shots": 20, "makes": 12, "swishes": 3, "banks": 1, "best_streak": 5, "runs": 1}, headers=auth(token))
    assert r.status_code == 200
    assert r.json() == {"xp": 200, "level": 2, "gained": 200, "capped": False, "achievements": ["first_bucket", "streak_5"]}
    me = client.get("/auth/me", headers=auth(token)).json()
    assert me["xp"] == 200 and me["level"] == 2


def test_gain_xp_rejects_impossible_stats(client):
    token, _ = make_guest(client, "Cheat")
    for body in ({"shots": 5, "makes": 6}, {"shots": 9, "makes": 4, "swishes": 3, "banks": 2}, {"shots": 9, "makes": 3, "best_streak": 4}, {"shots": 5000}):
        assert client.post("/xp", json=body, headers=auth(token)).status_code == 422


def test_daily_bonus_once_and_only_for_a_current_challenge(client):
    token, _ = make_guest(client, "Daily")
    today = f"daily{datetime.now(UTC).strftime('%Y%m%d')}"
    old = f"daily{(datetime.now(UTC) - timedelta(days=5)).strftime('%Y%m%d')}"
    assert client.post("/xp", json={"daily": today}, headers=auth(token)).json()["gained"] == 50
    assert client.post("/xp", json={"daily": today}, headers=auth(token)).json()["gained"] == 0
    assert client.post("/xp", json={"daily": old}, headers=auth(token)).json()["gained"] == 0


def test_solo_xp_is_capped_per_day(client, engine):
    token, user_id = make_guest(client, "Grinder")
    with Session(engine) as s:
        u = s.get(User, __import__("uuid").UUID(user_id))
        u.xp_day, u.xp_today = datetime.now(UTC).strftime("%Y%m%d"), SOLO_XP_DAY_CAP - 30
        s.add(u)
        s.commit()
    r = client.post("/xp", json={"shots": 10, "makes": 10}, headers=auth(token)).json()
    assert r["gained"] == 30 and r["capped"] is True


def test_loadout_is_shown_only_when_unlocked(client, engine):
    from app.game.cosmetics import BALL_LEVELS, TRAIL_LEVELS, usable_loadout

    # Same table as the frontend's cosmetics.ts
    assert BALL_LEVELS == {"classic": 1, "retro": 3, "gold": 6, "ice": 10, "lava": 15, "galaxy": 22}
    assert TRAIL_LEVELS == {"flame": 1, "blue": 4, "toxic": 8, "royal": 12, "rainbow": 18}
    assert usable_loadout({"ball": "gold", "trail": "blue"}, 5) == {"ball": "classic", "trail": "blue"}
    assert usable_loadout({"ball": "<script>", "trail": None}, 50) == {"ball": "classic", "trail": "flame"}

    token, _ = make_guest(client, "Skins")
    me = client.patch("/auth/me", json={"loadout": {"ball": "gold", "trail": "flame"}}, headers=auth(token)).json()
    assert me["loadout"] == {"ball": "classic", "trail": "flame"}  # level 1: gold is locked
    client.post("/xp", json={"shots": 100, "makes": 100}, headers=auth(token))  # 1,100 XP: level 6
    assert client.get("/auth/me", headers=auth(token)).json()["loadout"] == {"ball": "gold", "trail": "flame"}
