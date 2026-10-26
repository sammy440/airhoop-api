"""
In-memory rooms + WebSocket presence (README Week 6).

Lifecycle:  lobby ⇄ ready → in_progress → finished → (rematch) lobby
  • lobby → ready        automatically, when ≥2 players are connected and all are ready
  • ready → lobby        if anyone un-readies, disconnects, joins or leaves
  • ready → in_progress  host sends "start"
  • in_progress → finished  host sends "end" (Week 7 ends matches automatically)
  • finished → lobby     host sends "rematch"

Presence:
  • A dropped socket keeps its seat for `reconnect_grace_s` (reloads, Wi-Fi blips).
  • A socket silent for `heartbeat_timeout_s` (no message, not even "ping") is closed.
  • `sweep()` removes expired seats, hands the host role on, and deletes empty rooms.

State lives in one process (README: single instance first; Redis pub/sub later).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import WebSocket

from .config import Settings

Phase = Literal["lobby", "ready", "in_progress", "finished"]
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: easy to read aloud
CODE_LENGTH = 5

# WebSocket close codes (4000-4999 are application-defined)
CLOSE_HEARTBEAT = 4000
CLOSE_REPLACED = 4001
CLOSE_REMOVED = 4002


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

    @property
    def connected(self) -> bool:
        return self.ws is not None


@dataclass
class Room:
    code: str
    host_id: str
    mode: str
    difficulty: str
    max_players: int
    created_at: float
    phase: Phase = "lobby"
    players: dict[str, Player] = field(default_factory=dict)
    match_id: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "phase": self.phase,
            "mode": self.mode,
            "difficulty": self.difficulty,
            "max_players": self.max_players,
            "host_id": self.host_id,
            "players": [
                {"id": p.id, "nickname": p.nickname, "ready": p.ready, "connected": p.connected, "host": p.id == self.host_id}
                for p in self.players.values()
            ],
        }


Hook = Callable[[Room], Awaitable[None]]


class RoomManager:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.clock = clock
        self.rooms: dict[str, Room] = {}
        self._lock: asyncio.Lock | None = None
        # Persistence hooks (set by the app): create / close the Match row.
        self.on_start: Hook | None = None
        self.on_finish: Hook | None = None

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

    def create(self, user_id: str, nickname: str, mode: str, difficulty: str) -> Room:
        now = self.clock()
        room = Room(
            code=self._new_code(),
            host_id=user_id,
            mode=mode,
            difficulty=difficulty,
            max_players=self.settings.max_players,
            created_at=now,
        )
        # The creator holds a seat until their socket connects (or the grace period ends).
        room.players[user_id] = Player(id=user_id, nickname=nickname, joined_at=now, disconnected_at=now)
        self.rooms[room.code] = room
        return room

    def join(self, code: str, user_id: str, nickname: str) -> Room:
        room = self.get(code)
        if user_id in room.players:
            room.players[user_id].nickname = nickname
            return room  # rejoining your own seat is always allowed
        if room.phase in ("in_progress", "finished"):
            raise RoomError("room_in_progress", "That match has already started", 409)
        if len(room.players) >= room.max_players:
            raise RoomError("room_full", "That room is full", 409)
        now = self.clock()
        room.players[user_id] = Player(id=user_id, nickname=nickname, joined_at=now, disconnected_at=now)
        self._recompute_phase(room)
        return room

    # ── WebSocket-side operations ──

    async def connect(self, code: str, user_id: str, nickname: str, ws: WebSocket) -> Room:
        async with self.lock:
            room = self.join(code, user_id, nickname)
            player = room.players[user_id]
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
        return room

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
            await self._send(ws, {"type": "pong", "t": msg.get("t")})
            return True
        try:
            async with self.lock:
                changed = self._apply(room, player, kind, msg)
        except RoomError as e:
            await self._send(ws, {"type": "error", "code": e.code, "message": e.message})
            return True
        if kind == "leave":
            await self._presence(room, "left", player)
            await self.broadcast_state(room)
            await self._cleanup_if_empty(room)
            await self._safe_close(ws, 1000, "Left the room")
            return False
        if changed:
            await self._run_hooks(room, kind)
            await self.broadcast_state(room)
        return True

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
            room.phase = "in_progress"
        elif kind == "end":
            if not is_host:
                raise RoomError("not_host", "Only the host can end the match")
            if room.phase != "in_progress":
                raise RoomError("bad_phase", "No match in progress")
            room.phase = "finished"
        elif kind == "rematch":
            if not is_host:
                raise RoomError("not_host", "Only the host can start a rematch")
            if room.phase != "finished":
                raise RoomError("bad_phase", "The match isn't over yet")
            room.phase = "lobby"
            room.match_id = None
            for p in room.players.values():
                p.ready = False
        elif kind == "leave":
            self._remove(room, player.id)
        else:
            raise RoomError("unknown_message", f"Unknown message type: {kind!r}")
        self._recompute_phase(room)
        return True

    # ── Housekeeping ──

    async def sweep(self) -> None:
        """Expire abandoned seats, drop silent sockets, delete empty rooms."""
        now = self.clock()
        s = self.settings
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
            for p in expired:
                await self._presence(room, "left", p)
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
        elif kind == "end" and self.on_finish:
            await self.on_finish(room)

    # ── Messaging ──

    async def broadcast_state(self, room: Room) -> None:
        state = room.public()
        for p in list(room.players.values()):
            if p.ws is not None:
                await self._send(p.ws, {"type": "room_state", "room": state, "you": p.id})

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
