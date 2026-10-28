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

Presence:
  • A dropped socket keeps its seat for `reconnect_grace_s` (reloads, Wi-Fi blips).
  • A socket silent for `heartbeat_timeout_s` (no message, not even "ping") is closed.
  • `sweep()` removes expired seats, hands the host role on, and deletes empty rooms.

State lives in one process (single instance; Redis pub/sub when scaling out).
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
from .game.physics import ShotSim
from .game.rules import DIFFICULTIES
from .match import MatchEngine, MatchError

Phase = Literal["lobby", "ready", "in_progress", "finished"]
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: easy to read aloud
CODE_LENGTH = 5
BEST_OF = (1, 3, 5)
MODES = ("shootout", "race")

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
    avatar: dict[str, Any] | None = None

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
    race_seconds: int = 60
    phase: Phase = "lobby"
    players: dict[str, Player] = field(default_factory=dict)
    match_id: str | None = None
    match: MatchEngine | None = None
    best_of: int = 1
    # Games won in the current series, by player id (players who left keep theirs).
    series_wins: dict[str, int] = field(default_factory=dict)
    series_games: int = 0

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
                }
                for p in self.players.values()
            ],
        }


Hook = Callable[[Room], Awaitable[None]]
ThrowHook = Callable[[Room, str, dict[str, Any], ShotSim, int], Awaitable[None]]


class RoomManager:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.clock = clock
        self.rooms: dict[str, Room] = {}
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

    def join(self, code: str, user_id: str, nickname: str, avatar: dict[str, Any] | None = None) -> Room:
        room = self.get(code)
        if user_id in room.players:
            room.players[user_id].nickname = nickname
            room.players[user_id].avatar = avatar
            return room  # rejoining your own seat is always allowed
        if room.phase in ("in_progress", "finished"):
            raise RoomError("room_in_progress", "That match has already started", 409)
        if len(room.players) >= room.max_players:
            raise RoomError("room_full", "That room is full", 409)
        now = self.clock()
        room.players[user_id] = Player(id=user_id, nickname=nickname, joined_at=now, disconnected_at=now, avatar=avatar)
        self._recompute_phase(room)
        return room

    async def update_player(self, user_id: str, nickname: str, avatar: dict[str, Any] | None) -> None:
        """Profile edits show up right away in any room the player is in."""
        for room in list(self.rooms.values()):
            if (p := room.players.get(user_id)) is not None:
                p.nickname = nickname
                p.avatar = avatar
                await self.broadcast_state(room)

    # ── WebSocket-side operations ──

    async def connect(self, code: str, user_id: str, nickname: str, ws: WebSocket, avatar: dict[str, Any] | None = None) -> Room:
        async with self.lock:
            room = self.join(code, user_id, nickname, avatar)
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
        if room.match is not None:
            # Rejoining mid-match: rebuild the match view.
            for m in room.match.snapshot():
                await self._send(ws, {**m, "serverTime": self.clock()})
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
            await self._send(ws, {"type": "pong", "t": msg.get("t"), "serverTime": self.clock()})
            return True
        if kind == "throw":
            await self._throw(room, player, ws, msg)
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
            await self.on_throw(room, player.id, msg, sim, out[0]["points"])
        if room.match.finished:
            await self._finish_match(room)

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
        if self.on_finish:
            await self.on_finish(room)
        await self.broadcast_state(room)

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
            mode = room.mode if room.mode in ("shootout", "race") else "race"
            room.match = MatchEngine(mode, room.difficulty, list(room.players), self.clock(), race_seconds=room.race_seconds)
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
        if isinstance(race_seconds, bool) or not isinstance(race_seconds, int) or not 30 <= race_seconds <= 300:
            raise RoomError("invalid_settings", "Race length must be 30–300 seconds")
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
