"""Skill rating: Elo from refereed results, and quick match pairing by it."""

import uuid

from sqlmodel import Session

from app.game import rating
from app.match import COUNTDOWN_S
from app.models import User

from .conftest import auth, make_guest, receive_until
from .test_match import MAKE
from .test_match_ws import connect, lobby, start_match  # noqa: F401 - lobby is a fixture
from .test_quickmatch import poll


def test_elo_basics():
    assert rating.expected(1000, 1000) == 0.5
    even = rating.changes([("a", 1000, 0, 1), ("b", 1000, 0, 2)])
    assert even == {"a": 20, "b": -20}  # K 40 while provisional
    upset = rating.changes([("a", 1000, 50, 1), ("b", 1400, 50, 2)])
    assert upset["a"] > 12 and upset["b"] == -upset["a"]
    favourite = rating.changes([("a", 1400, 50, 1), ("b", 1000, 50, 2)])
    assert 0 < favourite["a"] < 4  # beating someone far below barely counts
    assert rating.changes([("a", 1000, 50, 1), ("b", 1000, 50, 1)]) == {"a": 0, "b": 0}  # shared win
    assert rating.changes([("a", 1000, 0, 1)]) == {"a": 0}


def test_four_players_average_their_pairs():
    c = rating.changes([("a", 1000, 50, 1), ("b", 1000, 50, 2), ("c", 1000, 50, 2), ("d", 1000, 50, 4)])
    assert c == {"a": 12, "b": 0, "c": 0, "d": -12}  # K 24 × (3 wins − 1.5 expected) / 3 pairs


def test_never_below_the_floor():
    assert rating.changes([("a", 110, 0, 1), ("b", 110, 0, 2)])["b"] == -10  # −20, stopped at 100


def test_window_widens_with_the_wait():
    assert rating.window(0) == 150
    assert rating.window(5) == 350
    assert rating.window(20) == float("inf")


def set_rating(engine, user_id, value):
    with Session(engine) as s:
        u = s.get(User, uuid.UUID(user_id))
        u.rating = value
        s.add(u)
        s.commit()


def test_quick_match_waits_for_someone_close_then_opens_up(client, engine, clock):
    (a, a_id), (b, b_id), (c, c_id) = [make_guest(client, n) for n in ("Ada", "Bayo", "Chi")]
    set_rating(engine, a_id, 1000)
    set_rating(engine, b_id, 1600)
    set_rating(engine, c_id, 1100)
    assert poll(client, a)["status"] == "waiting"
    assert poll(client, b)["status"] == "waiting"  # 600 apart: not yet
    matched = poll(client, c)  # 100 from Ada: paired straight away
    assert matched["status"] == "matched"
    players = {p["id"] for p in client.get(f"/rooms/{matched['code']}").json()["players"]}
    assert players == {a_id, c_id}

    # Bayo has waited since t=0 at 1600; Dee (1000) joins. 600 apart pairs once the longer wait allows it:
    # 150 + 40 × 15 s = 750 (at 10 s it's 550, not enough)
    d, _ = make_guest(client, "Dee")
    assert poll(client, d)["status"] == "waiting"
    waited = 0
    while True:
        clock.advance(5)
        waited += 5
        if poll(client, b)["status"] == "matched":
            break
        assert poll(client, d)["status"] == "waiting"
    assert waited == 15


def test_a_match_moves_both_ratings(client, lobby, clock):  # noqa: F811
    room = lobby("race")
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_match(a, b)
        clock.advance(COUNTDOWN_S + 0.5)
        a.send_json({"type": "throw", **MAKE, "t": clock.t})
        receive_until(a, "shot_result")
        clock.advance(31)
        client.portal.call(client.app.state.rooms.tick_matches)
        receive_until(a, "match_end")
        assert receive_until(a, "rating", limit=15) == {"type": "rating", "rating": 1020, "change": 20}
        assert receive_until(b, "rating", limit=15)["change"] == -20

    host = client.get("/auth/me", headers=auth(room["host"])).json()
    assert host["rating"] == 1020 and host["provisional"] is True
    history = client.get("/me/matches", headers=auth(room["host"])).json()
    assert [p["rating_change"] for p in history[0]["players"]] == [20, -20]


def test_an_aborted_match_is_unrated(client, lobby):  # noqa: F811
    room = lobby("race")
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_match(a, b)
        a.send_json({"type": "end"})
        receive_until(a, "match_end")
        receive_until(b, "match_end")
    assert client.get("/auth/me", headers=auth(room["host"])).json()["rating"] == 1000
