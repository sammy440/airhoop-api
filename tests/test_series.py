"""Best-of series across rematches, and host lobby settings."""

from app.match import COUNTDOWN_S

from .test_match import MAKE
from .test_match_ws import connect, lobby, start_match  # noqa: F401 - lobby is a fixture
from .conftest import receive_until


def win_race(client, clock, a, b):
    """Host makes one shot, the buzzer goes: host wins the game."""
    clock.advance(COUNTDOWN_S + 0.5)
    a.send_json({"type": "throw", **MAKE, "t": clock.t})
    receive_until(b, "shot_result")
    clock.advance(30)
    client.portal.call(client.app.state.rooms.tick_matches)
    end = receive_until(a, "match_end", limit=15)
    receive_until(b, "match_end", limit=15)
    return end


def last_state(ws, phase):
    while True:
        msg = receive_until(ws, "room_state", limit=15)
        if msg["room"]["phase"] == phase:
            return msg["room"]


def next_game(a, b):
    """Both ready up; host starts once the room says everyone is set."""
    a.send_json({"type": "ready"})
    b.send_json({"type": "ready"})
    last_state(a, "ready")
    a.send_json({"type": "start"})
    receive_until(a, "match_start", limit=15)
    receive_until(b, "match_start", limit=15)


def test_best_of_three_carries_wins_across_rematches(client, lobby, clock):
    room = lobby("race", best_of=3)
    host = room["host_id"]
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_match(a, b)
        assert win_race(client, clock, a, b)["winner"] == host
        state = last_state(a, "finished")
        assert state["series"] == {"best_of": 3, "wins": {host: 1}, "games": 1, "winner": None}

        a.send_json({"type": "rematch"})
        assert last_state(b, "lobby")["series"]["wins"] == {host: 1}  # rematch keeps the tally

        next_game(a, b)
        win_race(client, clock, a, b)
        state = last_state(a, "finished")
        assert state["series"]["wins"] == {host: 2} and state["series"]["winner"] == host

        a.send_json({"type": "rematch"})
        assert last_state(b, "lobby")["series"] == {"best_of": 3, "wins": {}, "games": 0, "winner": None}


def test_aborted_games_dont_count(client, lobby):
    room = lobby("race", best_of=3)
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_match(a, b)
        a.send_json({"type": "end"})
        assert last_state(b, "finished")["series"]["games"] == 0


def test_host_changes_the_game_in_the_lobby(client, lobby):
    room = lobby("race")
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        receive_until(a, "room_state")
        receive_until(b, "room_state")
        b.send_json({"type": "ready"})
        receive_until(a, "room_state")

        b.send_json({"type": "settings", "mode": "shootout"})
        assert receive_until(b, "error")["code"] == "not_host"

        a.send_json({"type": "settings", "mode": "shootout", "difficulty": "legend", "bestOf": 5})
        state = receive_until(b, "room_state", limit=15)["room"]
        assert (state["mode"], state["difficulty"], state["series"]["best_of"]) == ("shootout", "legend", 5)
        assert not any(p["ready"] for p in state["players"])  # everyone confirms the new game

        for bad in ({"mode": "golf"}, {"bestOf": 2}, {"raceSeconds": 5}, {"difficulty": "nope"}):
            a.send_json({"type": "settings", **bad})
            assert receive_until(a, "error")["code"] == "invalid_settings"


def test_settings_are_locked_during_a_match(client, lobby):
    room = lobby("race")
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        start_match(a, b)
        a.send_json({"type": "settings", "mode": "shootout"})
        assert receive_until(a, "error", limit=15)["code"] == "bad_phase"


def test_room_preview_shows_the_series(client, lobby):
    room = lobby("shootout", best_of=3)
    assert client.get(f"/rooms/{room['code']}").json()["series"]["best_of"] == 3
