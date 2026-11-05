"""Room chat: cleaned, rate-limited, quick chat only for strangers, and never stored."""

from sqlmodel import SQLModel

from app.chat import CHAT_BURST, MAX_CHARS, clean_text

from .conftest import receive_until
from .test_ws import connect, room  # noqa: F401  (fixture)


def test_clean_text():
    assert clean_text("  good   game\n") == "good game"
    assert clean_text("check www.example.com and https://x.co/abc now") == "check [link] and [link] now"
    assert clean_text("mail me: me@example.com") == "mail me: [hidden]"
    assert clean_text("what the fuck, SHIT shot") == "what the f***, S*** shot"
    assert clean_text("scunthorpe, cocktail, assassin") == "scunthorpe, cocktail, assassin"  # whole words only
    assert clean_text("a‮b​c") == "abc"  # bidi override and zero-width space removed
    assert clean_text("x" * 500) == "x" * MAX_CHARS
    for empty in ("", "   ", "\n\t", None, 42, ["hi"]):
        assert clean_text(empty) is None


def drain(ws, kind):
    """Messages of `kind` until the pong for a ping sent now."""
    ws.send_json({"type": "ping", "t": 1})
    got = []
    while (m := ws.receive_json())["type"] != "pong":
        if m["type"] == kind:
            got.append(m)
    return got


def test_friends_chat_freely_and_it_is_not_kept(client, room, clock):  # noqa: F811
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        receive_until(b, "room_state")
        b.send_json({"type": "chat", "text": "  gg, rematch?  www.spam.com "})
        got = receive_until(a, "chat")
        assert got["playerId"] == room["guest_id"] and got["text"] == "gg, rematch? [link]"
        assert receive_until(b, "chat")["text"] == "gg, rematch? [link]"  # the sender sees it too
        b.send_json({"type": "chat", "quick": "nice_shot"})
        assert receive_until(a, "chat")["quick"] == "nice_shot"

        b.send_json({"type": "chat", "text": "   "})
        assert receive_until(b, "error")["code"] == "invalid_chat"

        for i in range(CHAT_BURST + 2):
            b.send_json({"type": "chat", "text": f"msg {i}"})
        msgs = drain(b, "chat")
        assert len(msgs) == CHAT_BURST - 2  # two already used in this window
        clock.advance(10.1)
        b.send_json({"type": "chat", "text": "again"})
        assert [m["text"] for m in drain(b, "chat")] == ["again"]

    # Not stored: the room keeps no history, and a reconnect gets none back
    live = client.app.state.rooms.rooms[room["code"]]
    assert not any("again" in repr(v) for v in vars(live).values())
    assert not any("again" in repr(vars(p)) for p in live.players.values())
    with connect(client, room, "guest") as b:
        assert drain(b, "chat") == []
    # …and there's nowhere in the database it could be
    assert not any("chat" in name or "message" in name for name in SQLModel.metadata.tables)


def test_strangers_get_quick_chat_only(client, room):  # noqa: F811
    client.app.state.rooms.rooms[room["code"]].quick = True
    with connect(client, room, "host") as a, connect(client, room, "guest") as b:
        receive_until(b, "room_state")
        b.send_json({"type": "chat", "text": "hi there"})
        assert receive_until(b, "error")["code"] == "quick_chat_only"
        b.send_json({"type": "chat", "quick": "gg"})
        got = receive_until(a, "chat")
        assert got["playerId"] == room["guest_id"] and got["quick"] == "gg" and "text" not in got
