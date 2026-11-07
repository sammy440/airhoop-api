"""
In-memory rooms + WebSocket presence.

Lifecycle:  lobby ⇄ ready → in_progress → finished → (rematch) lobby
  • lobby → ready        automatically, when ≥2 players are connected and all are ready
  • ready → lobby        if anyone un-readies, disconnects, joins or leaves
  • ready → in_progress  host sends "start"
  • in_progress → finished  the match ends (or the host sends "end")
  • finished → lobby     host sends "rematch"

Series: a room plays best-of-1, 3 or 5. Wins carry over rematches until someone
reaches the target; the rematch after that starts a fresh series. The host can
change mode / difficulty / race length / series length in the lobby
("settings"), which un-readies everyone and resets the series.

Reactions: anyone in the room can send one of a fixed set ("react"), broadcast to
everyone as "reaction"; a few per player every few seconds, extras dropped.

Chat ("chat" → "chat", see chat.py): free text in rooms joined by code, quick chat only in
quick-match rooms. Never stored: relayed and forgotten, not logged, not kept for reconnects.

Presence:
  • A dropped socket keeps its seat for `reconnect_grace_s` (reloads, Wi-Fi blips).
  • A socket silent for `heartbeat_timeout_s` (no message, not even "ping") is closed.
  • `sweep()` removes expired seats, hands the host role on, and deletes empty rooms.

State lives in one process (single instance; Redis pub/sub when scaling out).
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import WebSocket

from .chat import CHAT_BURST, CHAT_WINDOW_S, QUICK_CHAT, clean_text
from .config import Settings
from .game.physics import ShotSim
from .game.rules import DIFFICULTIES
from .game import rating as rating_rules
from .match import MatchEngine, MatchError, valid_race_seconds

Phase = Literal["lobby", "ready", "in_progress", "finished"]
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: easy to read aloud
CODE_LENGTH = 5
BEST_OF = (1, 3, 5)
MODES = ("shootout", "race", "horse")
# Quick match: strangers play a race (both shoot at once, nobody waits for a turn).
QUICK_MODE = "race"
QUICK_RACE_S = 60
QUICK_FALLBACK_DIFFICULTY = "starter"
# Reactions are ids, never free text: the client maps them to emoji.
REACTIONS = ("fire", "clap", "laugh", "wow", "flex", "ball")
REACTION_BURST = 4  # at most this many …
REACTION_WINDOW_S = 3.0  # … per player in this window; extras are dropped quietly

# WebSocket close codes (4000-4999 are application-defined)
CLOSE_HEARTBEAT = 4000
CLOSE_REPLACED = 4001
CLOSE_REMOVED = 4002
CLOSE_NOT_FOUND = 4404


class RoomError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class Player:
    id: str
    nickname: str
    joined_at: float
    ready: bool = False
    ws: WebSocket | None = None
    last_seen: float = 0.0
    # Set while disconnected; the seat expires `reconnect_grace_s` after this.
    disconnected_at: float | None = None
    # Distinguishes a first "joined" from a "reconnected" presence event.
    ever_connected: bool = False
    avatar: dict[str, Any] | None = None
    # Recent reaction and chat times, for the rate limits (times only, never what was said)
    reacted_at: list[float] = field(default_factory=list)
    chatted_at: list[float] = field(default_factory=list)
    level: int = 1
    loadout: dict[str, str] | None = None

    @property
    def connected(self) -> bool:
        return self.ws is not None


@dataclass
class Spectator:
    """Someone watching a room: they see everything the players see, and can't act."""

    user_id: str
    nickname: str
    ws: WebSocket
    last_seen: float


# Watchers per room (quick-match rooms, between strangers, can't be watched).
MAX_SPECTATORS = 20


@dataclass
class Room:
    code: str
    host_id: str
    mode: str
    difficulty: str
    max_players: int
    created_at: float
    race_seconds: int = 60
    phase: Phase = "lobby"
    players: dict[str, Player] = field(default_factory=dict)
    match_id: str | None = None
    match: MatchEngine | None = None
    best_of: int = 1
    # Games won in the current series, by player id (players who left keep theirs).
    series_wins: dict[str, int] = field(default_factory=dict)
    series_games: int = 0
    # Made by quick match: both seats start ready, and the first game starts once both are connected.
    quick: bool = False
    auto_start: bool = False
    spectators: dict[str, Spectator] = field(default_factory=dict)

    @property
    def wins_needed(self) -> int:
        return self.best_of // 2 + 1

    @property
    def series_winner(self) -> str | None:
        return next((pid for pid, w in self.series_wins.items() if w >= self.wins_needed), None)

    def reset_series(self) -> None:
        self.series_wins = {}
        self.series_games = 0

    def public(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "phase": self.phase,
            "mode": self.mode,
            "difficulty": self.difficulty,
            "max_players": self.max_players,
            "race_seconds": self.race_seconds,
            "host_id": self.host_id,
            "quick": self.quick,
            "spectators": len(self.spectators),
            "series": {
                "best_of": self.best_of,
                "wins": self.series_wins,
                "games": self.series_games,
                "winner": self.series_winner,
            },
            "players": [
                {
                    "id": p.id,
                    "nickname": p.nickname,
                    "avatar": p.avatar,
                    "ready": p.ready,
                    "connected": p.connected,
                    "host": p.id == self.host_id,
                    "level": p.level,
                    "loadout": p.loadout,
                }
                for p in self.players.values()
            ],
        }


@dataclass
class Invite:
    """An invite to a room, from someone you've played before. In memory only, like rooms."""

    from_id: str
    nickname: str
    avatar: dict[str, Any] | None
    code: str
    at: float


# An invite lasts this long (s), unless its room starts, fills up, goes, or its sender leaves it first.
INVITE_TTL_S = 600
# Invites a player can be holding at once (one per sender; the oldest go first).
MAX_INVITES = 10


@dataclass
class QueueEntry:
    """A player waiting for a quick match. They poll; one who stops polling drops out."""

    user_id: str
    nickname: str
    avatar: dict[str, Any] | None
    difficulty: str  # a difficulty, or "any"
    since: float
    last_poll: float
    # Set when someone else's poll paired them; handed over on their next poll.
    room_code: str | None = None
    rating: int = rating_rules.DEFAULT


Hook = Callable[[Room], Awaitable[None]]
# (room, player id, the throw message, the server's simulation, the shot_result sent to the room)
ThrowHook = Callable[[Room, str, dict[str, Any], ShotSim, dict[str, Any]], Awaitable[None]]


class RoomManager:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.clock = clock
        self.rooms: dict[str, Room] = {}
        self.queue: dict[str, QueueEntry] = {}
        # Invites by who they're for, then by who sent them
        self.invites: dict[str, dict[str, Invite]] = {}
        # REST handlers run in a thread pool: two polls must never grab the same partner.
        self._queue_lock = threading.Lock()
        self._lock: asyncio.Lock | None = None
        # Persistence hooks (set by the app): create / close the Match row.
        self.on_start: Hook | None = None
        self.on_finish: Hook | None = None
        self.on_throw: ThrowHook | None = None

    @property
    def lock(self) -> asyncio.Lock:
        # Created lazily so it binds to the running event loop.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ── REST-side operations ──

    def _new_code(self) -> str:
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self.rooms:
                return code

    def get(self, code: str) -> Room:
        room = self.rooms.get(code.upper())
        if not room:
            raise RoomError("room_not_found", "No room with that code", 404)
        return room

    def create(
        self,
        user_id: str,
        nickname: str,
        mode: str,
        difficulty: str,
        race_seconds: int = 60,
        avatar: dict[str, Any] | None = None,
        best_of: int = 1,
    ) -> Room:
        now = self.clock()
        room = Room(
            code=self._new_code(),
            host_id=user_id,
            mode=mode,
            difficulty=difficulty,
            max_players=self.settings.max_players,
            created_at=now,
            race_seconds=race_seconds,
            best_of=best_of if best_of in BEST_OF else 1,
        )
        # The creator holds a seat until their socket connects (or the grace period ends).
        room.players[user_id] = Player(id=user_id, nickname=nickname, joined_at=now, disconnected_at=now, avatar=avatar)
        self.rooms[room.code] = room
        return room

    # ── Quick match ──

    def quick_match(
        self, user_id: str, nickname: str, difficulty: str, avatar: dict[str, Any] | None = None, rating: int = rating_rules.DEFAULT
    ) -> dict[str, Any]:
        """
        One poll from a player looking for a game: pairs them with the longest-waiting compatible
        player of similar rating (the gap allowed widens as either waits), or keeps them in the queue. Returns {"status": "matched", "code"} or
        {"status": "waiting", "waiting": players in the queue, "waited_s"}.
        """
        with self._queue_lock:
            now = self.clock()
            self._expire_queue(now)
            entry = self.queue.get(user_id)
            if entry is not None and entry.room_code is not None:
                del self.queue[user_id]
                room = self.rooms.get(entry.room_code)
                if room is not None and user_id in room.players:
                    return {"status": "matched", "code": room.code}
                entry = None  # that room's gone (the other player left): queue again
            if entry is None or entry.difficulty != difficulty:
                entry = QueueEntry(user_id, nickname, avatar, difficulty, since=entry.since if entry else now, last_poll=now)
                self.queue[user_id] = entry
            entry.last_poll, entry.nickname, entry.avatar, entry.rating = now, nickname, avatar, rating

            def close_enough(e: QueueEntry) -> bool:
                waited = max(now - entry.since, now - e.since)
                return abs(e.rating - rating) <= rating_rules.window(waited)

            partner = min(
                (
                    e
                    for e in self.queue.values()
                    if e.user_id != user_id and e.room_code is None and _compatible(e.difficulty, difficulty) and close_enough(e)
                ),
                key=lambda e: e.since,
                default=None,
            )
            if partner is None:
                waiting = sum(1 for e in self.queue.values() if e.room_code is None)
                return {"status": "waiting", "waiting": waiting, "waited_s": round(now - entry.since, 1)}

            chosen = next((d for d in (difficulty, partner.difficulty) if d != "any"), QUICK_FALLBACK_DIFFICULTY)
            room = self.create(partner.user_id, partner.nickname, QUICK_MODE, chosen, QUICK_RACE_S, avatar=partner.avatar)
            self.join(room.code, user_id, nickname, avatar)
            room.quick = room.auto_start = True
            for p in room.players.values():
                p.ready = True
            partner.room_code = room.code
            del self.queue[user_id]
            return {"status": "matched", "code": room.code}

    # ── Invites ──

    def invite(self, from_id: str, nickname: str, avatar: dict[str, Any] | None, to_id: str, code: str) -> None:
        """Invites `to_id` to a room the sender is in (still in the lobby, with a seat free)."""
        room = self.get(code)
        if from_id not in room.players:
            raise RoomError("not_in_room", "You're not in that room", 403)
        if to_id == from_id or to_id in room.players:
            raise RoomError("already_in_room", "They're already in the room", 409)
        if room.quick or room.phase not in ("lobby", "ready") or len(room.players) >= room.max_players:
            raise RoomError("room_not_open", "That room can't take another player right now", 409)
        box = self.invites.setdefault(to_id, {})
        box.pop(from_id, None)
        box[from_id] = Invite(from_id, nickname, avatar, room.code, self.clock())
        while len(box) > MAX_INVITES:
            box.pop(next(iter(box)))

    def invites_for(self, user_id: str) -> list[dict[str, Any]]:
        """Your invites that can still be taken up, newest first (stale ones are dropped)."""
        box = self.invites.get(user_id, {})
        now = self.clock()
        live = []
        for from_id, inv in list(box.items()):
            room = self.rooms.get(inv.code)
            ok = (
                room is not None
                and now - inv.at <= INVITE_TTL_S
                and from_id in room.players
                and user_id not in room.players
                and room.phase in ("lobby", "ready")
                and len(room.players) < room.max_players
            )
            if not ok:
                del box[from_id]
                continue
            assert room is not None
            live.append(
                {
                    "from_id": from_id,
                    "nickname": inv.nickname,
                    "avatar": inv.avatar,
                    "code": room.code,
                    "mode": room.mode,
                    "difficulty": room.difficulty,
                    "race_seconds": room.race_seconds,
                    "best_of": room.best_of,
                    "players": len(room.players),
                    "age_s": round(now - inv.at, 1),
                }
            )
        if not box:
            self.invites.pop(user_id, None)
        return sorted(live, key=lambda i: i["age_s"])

    def dismiss_invite(self, user_id: str, from_id: str) -> None:
        box = self.invites.get(user_id, {})
        box.pop(from_id, None)

    def leave_queue(self, user_id: str) -> None:
        with self._queue_lock:
            self.queue.pop(user_id, None)

    def _expire_queue(self, now: float) -> None:
        for e in list(self.queue.values()):
            # A paired player who never came back for their room loses it with their seat.
            limit = self.settings.reconnect_grace_s if e.room_code else self.settings.queue_timeout_s
            if now - e.last_poll > limit:
                del self.queue[e.user_id]

    def join(self, code: str, user_id: str, nickname: str, avatar: dict[str, Any] | None = None) -> Room:
        room = self.get(code)
        if user_id in room.players:
            room.players[user_id].nickname = nickname
            room.players[user_id].avatar = avatar
            return room  # rejoining your own seat is always allowed
        if room.phase in ("in_progress", "finished"):
            raise RoomError("room_in_progress", "That match has already started", 409)
        # Taking up an invite (or joining by code anyway) uses up the invites to this room
        for from_id, inv in list(self.invites.get(user_id, {}).items()):
            if inv.code == room.code:
                del self.invites[user_id][from_id]
        if len(room.players) >= room.max_players:
            raise RoomError("room_full", "That room is full", 409)
        now = self.clock()
        room.players[user_id] = Player(id=user_id, nickname=nickname, joined_at=now, disconnected_at=now, avatar=avatar)
        self._recompute_phase(room)
        return room

    async def update_player(self, user_id: str, nickname: str, avatar: dict[str, Any] | None, loadout: dict[str, str] | None = None) -> None:
        """Profile edits show up right away in any room the player is in."""
        for room in list(self.rooms.values()):
            if (p := room.players.get(user_id)) is not None:
                p.nickname = nickname
                p.avatar = avatar
                if loadout is not None:
                    p.loadout = loadout
                await self.broadcast_state(room)

    # ── WebSocket-side operations ──

    async def connect(
        self,
        code: str,
        user_id: str,
        nickname: str,
        ws: WebSocket,
        avatar: dict[str, Any] | None = None,
        level: int = 1,
        loadout: dict[str, str] | None = None,
    ) -> Room:
        async with self.lock:
            room = self.join(code, user_id, nickname, avatar)
            player = room.players[user_id]
            player.level = level
            player.loadout = loadout
            old = player.ws
            first_connect = not player.ever_connected
            was_away = player.ever_connected and old is None
            player.ever_connected = True
            player.ws = ws
            player.last_seen = self.clock()
            player.disconnected_at = None
            self._recompute_phase(room)
        if old is not None and old is not ws:
            # Same user opened a second tab: the newest connection wins.
            await self._safe_close(old, CLOSE_REPLACED, "Connected from another tab")
        event = "joined" if first_connect else "reconnected" if was_away else None
        if event:
            await self._presence(room, event, player)
        await self.broadcast_state(room)
        if room.match is not None:
            # Rejoining mid-match: rebuild the match view.
            for m in room.match.snapshot():
                await self._send(ws, {**m, "serverTime": self.clock()})
        elif room.auto_start and room.phase == "ready":
            await self._auto_start(room)
        return room

    async def _auto_start(self, room: Room) -> None:
        """Quick-match rooms start their first game as soon as both players are connected."""
        async with self.lock:
            if not room.auto_start or room.phase != "ready":
                return
            room.auto_start = False
            self._begin_match(room)
        await self._run_hooks(room, "start")
        await self.broadcast_state(room)
        assert room.match is not None
        await self._match_out(room, [room.match.start_msg(), room.match.scoreboard(), *filter(None, [room.match.turn_msg()])])

    # ── Spectators ──

    async def watch(self, code: str, user_id: str, nickname: str, ws: WebSocket) -> Room:
        """Starts watching a room: its state now, the match so far, and everything after."""
        async with self.lock:
            room = self.get(code)
            if room.quick:
                raise RoomError("not_watchable", "Quick matches can't be watched", 403)
            if user_id in room.players:
                raise RoomError("already_in_room", "You're playing in this room", 409)
            old = room.spectators.get(user_id)
            if old is None and len(room.spectators) >= MAX_SPECTATORS:
                raise RoomError("watchers_full", "That room has as many watchers as it can take", 409)
            room.spectators[user_id] = Spectator(user_id, nickname, ws, self.clock())
        if old is not None and old.ws is not ws:
            await self._safe_close(old.ws, CLOSE_REPLACED, "Watching from another tab")
        await self.broadcast_state(room)
        if room.match is not None:
            for m in room.match.snapshot():
                await self._send(ws, {**m, "serverTime": self.clock()})
        return room

    async def unwatch(self, code: str, user_id: str, ws: WebSocket) -> None:
        room = self.rooms.get(code)
        if room is None:
            return
        s = room.spectators.get(user_id)
        if s is not None and s.ws is ws:
            del room.spectators[user_id]
            await self.broadcast_state(room)

    async def handle_spectator(self, code: str, user_id: str, ws: WebSocket, msg: dict[str, Any]) -> bool:
        """A watcher's message: ping and leave only. Returns False when the socket should stop reading."""
        room = self.rooms.get(code)
        s = room.spectators.get(user_id) if room else None
        if s is None or s.ws is not ws:
            return False
        s.last_seen = self.clock()
        kind = msg.get("type")
        if kind == "ping":
            await self._send(ws, {"type": "pong", "t": msg.get("t"), "serverTime": self.clock()})
            return True
        if kind == "leave":
            await self.unwatch(code, user_id, ws)
            await self._safe_close(ws, 1000, "Stopped watching")
            return False
        await self._send(ws, {"type": "error", "code": "spectating", "message": "You're watching this room"})
        return True

    async def disconnect(self, code: str, user_id: str, ws: WebSocket) -> None:
        async with self.lock:
            room = self.rooms.get(code)
            player = room.players.get(user_id) if room else None
            if not room or not player or player.ws is not ws:
                return  # already replaced or removed
            player.ws = None
            player.disconnected_at = self.clock()
            self._recompute_phase(room)
        await self._presence(room, "disconnected", player)
        await self.broadcast_state(room)

    async def handle(self, code: str, user_id: str, ws: WebSocket, msg: dict[str, Any]) -> bool:
        """Applies one client message. Returns False when the socket should stop reading."""
        room = self.rooms.get(code)
        player = room.players.get(user_id) if room else None
        if not room or not player or player.ws is not ws:
            return False
        player.last_seen = self.clock()
        kind = msg.get("type")

        if kind == "ping":
            await self._send(ws, {"type": "pong", "t": msg.get("t"), "serverTime": self.clock()})
            return True
        if kind == "throw":
            await self._throw(room, player, ws, msg)
            return True
        if kind == "react":
            await self._react(room, player, ws, msg)
            return True
        if kind == "chat":
            await self._chat(room, player, ws, msg)
            return True
        try:
            async with self.lock:
                changed = self._apply(room, player, kind, msg)
        except RoomError as e:
            await self._send(ws, {"type": "error", "code": e.code, "message": e.message})
            return True
        if kind == "leave":
            await self._presence(room, "left", player)
            await self._forfeit(room, player.id)
            await self.broadcast_state(room)
            await self._cleanup_if_empty(room)
            await self._safe_close(ws, 1000, "Left the room")
            return False
        if changed:
            await self._run_hooks(room, kind)
            await self.broadcast_state(room)
            if kind == "start" and room.match:
                await self._match_out(room, [room.match.start_msg(), room.match.scoreboard(), *filter(None, [room.match.turn_msg()])])
            elif kind == "end" and room.match and room.match.result:
                await self._match_out(room, [room.match.result])
            if room.auto_start and room.phase == "ready":
                await self._auto_start(room)  # quick match: someone un-readied, then readied again
        return True

    async def _throw(self, room: Room, player: Player, ws: WebSocket, msg: dict[str, Any]) -> None:
        if room.phase != "in_progress" or room.match is None:
            await self._send(ws, {"type": "error", "code": "not_playing", "message": "No match in progress"})
            return
        try:
            async with self.lock:
                out, sim = room.match.throw(player.id, msg, self.clock())
        except MatchError as e:
            await self._send(ws, {"type": "error", "code": e.code, "message": e.message})
            return
        await self._match_out(room, out)
        if self.on_throw:
            await self.on_throw(room, player.id, msg, sim, out[0])
        if room.match.finished:
            await self._finish_match(room)

    async def _react(self, room: Room, player: Player, ws: WebSocket, msg: dict[str, Any]) -> None:
        reaction = msg.get("reaction")
        if reaction not in REACTIONS:
            await self._send(ws, {"type": "error", "code": "invalid_reaction", "message": "Unknown reaction"})
            return
        now = self.clock()
        player.reacted_at = [t for t in player.reacted_at if now - t < REACTION_WINDOW_S]
        if len(player.reacted_at) >= REACTION_BURST:
            return  # spam: dropped without a fuss
        player.reacted_at.append(now)
        await self._match_out(room, [{"type": "reaction", "playerId": player.id, "reaction": reaction}])

    async def _chat(self, room: Room, player: Player, ws: WebSocket, msg: dict[str, Any]) -> None:
        """Checks a message and passes it to everyone in the room. Nothing is kept: not the text, not a history."""
        if room.quick:
            # Strangers: quick chat only
            quick = msg.get("quick")
            if quick not in QUICK_CHAT:
                await self._send(ws, {"type": "error", "code": "quick_chat_only", "message": "Quick match has quick chat only"})
                return
            out: dict[str, Any] = {"type": "chat", "playerId": player.id, "quick": quick}
        elif "quick" in msg:
            if msg["quick"] not in QUICK_CHAT:
                await self._send(ws, {"type": "error", "code": "invalid_chat", "message": "Unknown quick chat"})
                return
            out = {"type": "chat", "playerId": player.id, "quick": msg["quick"]}
        else:
            text = clean_text(msg.get("text"))
            if text is None:
                await self._send(ws, {"type": "error", "code": "invalid_chat", "message": "Type a message first"})
                return
            out = {"type": "chat", "playerId": player.id, "text": text}
        now = self.clock()
        player.chatted_at = [t for t in player.chatted_at if now - t < CHAT_WINDOW_S]
        if len(player.chatted_at) >= CHAT_BURST:
            await self._send(ws, {"type": "error", "code": "chat_too_fast", "message": "Slow down a little"})
            return
        player.chatted_at.append(now)
        await self._match_out(room, [out])

    async def _forfeit(self, room: Room, user_id: str) -> None:
        if room.phase != "in_progress" or room.match is None:
            return
        out = room.match.forfeit(user_id, self.clock())
        await self._match_out(room, out)
        if room.match.finished:
            await self._finish_match(room)

    async def _finish_match(self, room: Room) -> None:
        room.phase = "finished"
        self._record_series(room)
        await self._on_finish_once(room)
        await self.broadcast_state(room)

    async def _on_finish_once(self, room: Room) -> None:
        """The finish hook (scores, XP) runs once per match, even when the buzzer and a last throw finish it together."""
        m = room.match
        if not self.on_finish or (m is not None and m.finish_recorded):
            return
        if m is not None:
            m.finish_recorded = True
        await self.on_finish(room)

    async def tick_matches(self) -> None:
        """Shot clocks and buzzers for every running match."""
        now = self.clock()
        for room in list(self.rooms.values()):
            m = room.match
            if room.phase != "in_progress" or m is None or m.finished:
                continue
            out = m.tick(now, lambda pid, r=room: pid in r.players and r.players[pid].connected)
            if out:
                await self._match_out(room, out)
            if m.finished:
                await self._finish_match(room)

    async def run_ticker(self) -> None:
        while True:
            await asyncio.sleep(self.settings.tick_interval_s)
            try:
                await self.tick_matches()
            except Exception:
                import logging

                logging.getLogger("airhoops.rooms").exception("tick failed")

    async def _match_out(self, room: Room, msgs: list[dict[str, Any]]) -> None:
        now = self.clock()
        for m in msgs:
            payload = {**m, "serverTime": now}
            for p in list(room.players.values()):
                if p.ws is not None:
                    await self._send(p.ws, payload)
            for s in list(room.spectators.values()):
                await self._send(s.ws, payload)

    def _apply(self, room: Room, player: Player, kind: Any, msg: dict[str, Any]) -> bool:
        is_host = player.id == room.host_id
        if kind == "ready":
            if room.phase not in ("lobby", "ready"):
                raise RoomError("bad_phase", "You can only ready up in the lobby")
            player.ready = bool(msg.get("ready", True))
        elif kind == "start":
            if not is_host:
                raise RoomError("not_host", "Only the host can start")
            if room.phase != "ready":
                raise RoomError("not_ready", "Everyone must be connected and ready (2+ players)")
            self._begin_match(room)
        elif kind == "end":
            if not is_host:
                raise RoomError("not_host", "Only the host can end the match")
            if room.phase != "in_progress":
                raise RoomError("bad_phase", "No match in progress")
            room.phase = "finished"
            if room.match and not room.match.finished:
                room.match.abort()
            self._record_series(room)
        elif kind == "rematch":
            if not is_host:
                raise RoomError("not_host", "Only the host can start a rematch")
            if room.phase != "finished":
                raise RoomError("bad_phase", "The match isn't over yet")
            room.phase = "lobby"
            room.match_id = None
            room.match = None
            if room.series_winner is not None:
                room.reset_series()  # that series is decided: the next game starts a new one
            for p in room.players.values():
                p.ready = False
        elif kind == "settings":
            if not is_host:
                raise RoomError("not_host", "Only the host can change the game")
            if room.phase not in ("lobby", "ready"):
                raise RoomError("bad_phase", "Change the game between matches")
            self._apply_settings(room, msg)
        elif kind == "leave":
            self._remove(room, player.id)
        else:
            raise RoomError("unknown_message", f"Unknown message type: {kind!r}")
        self._recompute_phase(room)
        return True

    def _begin_match(self, room: Room) -> None:
        room.phase = "in_progress"
        room.auto_start = False
        mode = room.mode if room.mode in MODES else "race"
        room.match = MatchEngine(mode, room.difficulty, list(room.players), self.clock(), race_seconds=room.race_seconds)

    @staticmethod
    def _apply_settings(room: Room, msg: dict[str, Any]) -> None:
        mode = msg.get("mode", room.mode)
        difficulty = msg.get("difficulty", room.difficulty)
        race_seconds = msg.get("raceSeconds", room.race_seconds)
        best_of = msg.get("bestOf", room.best_of)
        if mode not in MODES:
            raise RoomError("invalid_settings", "Unknown mode")
        if difficulty not in DIFFICULTIES:
            raise RoomError("invalid_settings", "Unknown difficulty")
        if not valid_race_seconds(race_seconds):
            raise RoomError("invalid_settings", "Race length must be 30 s, 90 s or whole minutes up to 30")
        if best_of not in BEST_OF or isinstance(best_of, bool):
            raise RoomError("invalid_settings", "Series must be best of 1, 3 or 5")
        changed = (mode, difficulty, race_seconds, best_of) != (room.mode, room.difficulty, room.race_seconds, room.best_of)
        if not changed:
            return
        room.mode, room.difficulty, room.race_seconds, room.best_of = mode, difficulty, race_seconds, best_of
        room.reset_series()  # a different game is a different contest
        # Everyone confirms the new game before it can start.
        for p in room.players.values():
            p.ready = False

    @staticmethod
    def _record_series(room: Room) -> None:
        """Counts a finished game towards the series (aborted games don't count)."""
        m = room.match
        if m is None or m.result is None or m.series_recorded:
            return
        m.series_recorded = True
        if m.result["reason"] == "aborted":
            return
        room.series_games += 1
        if (winner := m.result["winner"]) is not None:
            room.series_wins[winner] = room.series_wins.get(winner, 0) + 1

    # ── Housekeeping ──

    async def sweep(self) -> None:
        """Expire abandoned seats, drop silent sockets, delete empty rooms."""
        now = self.clock()
        s = self.settings
        with self._queue_lock:
            self._expire_queue(now)
        for room in list(self.rooms.values()):
            stale: list[Player] = []
            expired: list[Player] = []
            async with self.lock:
                for p in list(room.players.values()):
                    if p.connected and now - p.last_seen > s.heartbeat_timeout_s:
                        stale.append(p)
                    elif not p.connected and p.disconnected_at is not None and now - p.disconnected_at > s.reconnect_grace_s:
                        expired.append(p)
                        self._remove(room, p.id)
                self._recompute_phase(room)
            for p in stale:
                ws = p.ws
                if ws is not None:
                    await self._safe_close(ws, CLOSE_HEARTBEAT, "No heartbeat")
                    await self.disconnect(room.code, p.id, ws)
            for sp in [sp for sp in room.spectators.values() if now - sp.last_seen > s.heartbeat_timeout_s]:
                room.spectators.pop(sp.user_id, None)
                await self._safe_close(sp.ws, CLOSE_HEARTBEAT, "No heartbeat")
            for p in expired:
                await self._presence(room, "left", p)
                await self._forfeit(room, p.id)
            if expired:
                await self.broadcast_state(room)
            await self._cleanup_if_empty(room)

    async def run_sweeper(self) -> None:
        while True:
            await asyncio.sleep(self.settings.sweep_interval_s)
            try:
                await self.sweep()
            except Exception:  # never let housekeeping kill the server
                import logging

                logging.getLogger("airhoops.rooms").exception("sweep failed")

    async def _cleanup_if_empty(self, room: Room) -> None:
        if not room.players and self.rooms.get(room.code) is room:
            del self.rooms[room.code]
            for s in list(room.spectators.values()):
                await self._safe_close(s.ws, CLOSE_NOT_FOUND, "The room has closed")
            room.spectators.clear()

    def _remove(self, room: Room, user_id: str) -> None:
        room.players.pop(user_id, None)
        if room.host_id == user_id and room.players:
            # Hand hosting to whoever has been here longest.
            room.host_id = min(room.players.values(), key=lambda p: p.joined_at).id

    def _recompute_phase(self, room: Room) -> None:
        if room.phase not in ("lobby", "ready"):
            return
        players = list(room.players.values())
        everyone_set = len(players) >= 2 and all(p.connected and p.ready for p in players)
        room.phase = "ready" if everyone_set else "lobby"

    async def _run_hooks(self, room: Room, kind: Any) -> None:
        if kind == "start" and self.on_start:
            await self.on_start(room)
        elif kind == "end":
            await self._on_finish_once(room)

    # ── Messaging ──

    async def broadcast_state(self, room: Room) -> None:
        state = room.public()
        for p in list(room.players.values()):
            if p.ws is not None:
                await self._send(p.ws, {"type": "room_state", "room": state, "you": p.id})
        for s in list(room.spectators.values()):
            await self._send(s.ws, {"type": "room_state", "room": state, "you": None, "spectating": True})

    async def _presence(self, room: Room, event: str, player: Player) -> None:
        msg = {"type": "presence", "event": event, "player": {"id": player.id, "nickname": player.nickname}}
        for p in list(room.players.values()):
            if p.ws is not None and p.id != player.id:
                await self._send(p.ws, msg)

    @staticmethod
    async def _send(ws: WebSocket, msg: dict[str, Any]) -> None:
        try:
            await ws.send_json(msg)
        except Exception:
            pass  # socket already gone; disconnect handling will catch up

    @staticmethod
    async def _safe_close(ws: WebSocket, code: int, reason: str) -> None:
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            pass


def _compatible(a: str, b: str) -> bool:
    return a == b or "any" in (a, b)
