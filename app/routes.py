"""REST endpoints (README Week 6)."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from sqlalchemy import func
from sqlmodel import Session, select

from .auth import CurrentUser, SessionDep, create_guest, user_from_token
from .db import get_engine
from .models import Score, User
from .rooms import RoomError, RoomManager
from .schemas import GuestCreate, GuestOut, LeaderboardEntry, Rename, RoomCreate, RoomOut, ScoreCreate, UserOut

router = APIRouter()


def manager_of(request: Request | WebSocket) -> RoomManager:
    return request.app.state.rooms


def room_error(e: RoomError) -> HTTPException:
    return HTTPException(e.status, {"code": e.code, "message": e.message})


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Guest auth ──


@router.post("/auth/guest", status_code=status.HTTP_201_CREATED)
def guest(body: GuestCreate, session: SessionDep) -> GuestOut:
    user, token = create_guest(session, body.nickname)
    return GuestOut(user=UserOut(id=user.id, nickname=user.nickname), token=token)


@router.get("/auth/me")
def me(user: CurrentUser) -> UserOut:
    return UserOut(id=user.id, nickname=user.nickname)


@router.patch("/auth/me")
def rename(body: Rename, user: CurrentUser, session: SessionDep) -> UserOut:
    user.nickname = body.nickname
    session.add(user)
    session.commit()
    return UserOut(id=user.id, nickname=user.nickname)


# ── Rooms ──


@router.post("/rooms", status_code=status.HTTP_201_CREATED)
def create_room(body: RoomCreate, user: CurrentUser, request: Request) -> RoomOut:
    room = manager_of(request).create(str(user.id), user.nickname, body.mode, body.difficulty)
    return RoomOut(**room.public())


@router.get("/rooms/{code}")
def get_room(code: str, request: Request) -> RoomOut:
    try:
        return RoomOut(**manager_of(request).get(code).public())
    except RoomError as e:
        raise room_error(e) from e


@router.post("/rooms/{code}/join")
def join_room(code: str, user: CurrentUser, request: Request) -> RoomOut:
    try:
        return RoomOut(**manager_of(request).join(code, str(user.id), user.nickname).public())
    except RoomError as e:
        raise room_error(e) from e


# ── Scores / leaderboard ──


@router.post("/scores", status_code=status.HTTP_201_CREATED)
def submit_score(body: ScoreCreate, user: CurrentUser, session: SessionDep) -> dict[str, str]:
    # Solo scores are self-reported for now; multiplayer scores become
    # server-verified in Week 7 (the server re-simulates every throw).
    session.add(Score(user_id=user.id, mode=body.mode, difficulty=body.difficulty, score=body.score))
    session.commit()
    return {"status": "saved"}


PERIODS = {"day": timedelta(days=1), "week": timedelta(days=7), "all": None}


@router.get("/leaderboard")
def leaderboard(
    session: SessionDep,
    mode: Annotated[str, Query(pattern=r"^(free|streak|timed\d{2,4})$")] = "timed60",
    difficulty: Annotated[str | None, Query(pattern=r"^(rookie|starter|allstar|legend)$")] = None,
    period: Literal["day", "week", "all"] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[LeaderboardEntry]:
    best = func.max(Score.score).label("best")
    games = func.count(Score.id).label("games")
    q = select(Score.user_id, User.nickname, best, games).join(User, User.id == Score.user_id).where(Score.mode == mode)
    if difficulty:
        q = q.where(Score.difficulty == difficulty)
    if PERIODS[period]:
        q = q.where(Score.created_at >= datetime.now(UTC) - PERIODS[period])
    q = q.group_by(Score.user_id, User.nickname).order_by(best.desc(), games.asc()).limit(limit)
    return [
        LeaderboardEntry(rank=i + 1, user_id=row.user_id, nickname=row.nickname, best=row.best, games=row.games)
        for i, row in enumerate(session.exec(q).all())
    ]


# ── WebSocket rooms ──


@router.websocket("/ws/{code}")
async def room_socket(ws: WebSocket, code: str, token: str | None = None) -> None:
    """
    Connect with `/ws/{CODE}?token=<guest token>`.
    Client → server: {"type": "ping", "t"}, {"type": "ready", "ready"}, {"type": "start"},
                     {"type": "end"}, {"type": "rematch"}, {"type": "leave"}
    Server → client: {"type": "room_state", "room", "you"}, {"type": "presence", "event", "player"},
                     {"type": "pong", "t"}, {"type": "error", "code", "message"}
    """
    await ws.accept()
    manager = manager_of(ws)
    code = code.upper()
    with Session(get_engine()) as session:
        user = user_from_token(session, token)
        identity = (str(user.id), user.nickname) if user else None
    if identity is None:
        await ws.send_json({"type": "error", "code": "unauthorized", "message": "Missing or invalid guest token"})
        await ws.close(code=4401)
        return
    user_id, nickname = identity
    try:
        await manager.connect(code, user_id, nickname, ws)
    except RoomError as e:
        await ws.send_json({"type": "error", "code": e.code, "message": e.message})
        await ws.close(code=4404 if e.status == 404 else 4409)
        return

    try:
        while True:
            try:
                msg = await ws.receive_json()
            except ValueError:
                await ws.send_json({"type": "error", "code": "bad_json", "message": "Messages must be JSON objects"})
                continue
            if not isinstance(msg, dict):
                await ws.send_json({"type": "error", "code": "bad_json", "message": "Messages must be JSON objects"})
                continue
            if not await manager.handle(code, user_id, ws, msg):
                break  # the player left; the server closed the socket
    except WebSocketDisconnect:
        pass
    finally:
        # Shielded: even if this handler is being cancelled, everyone else must
        # still hear that this player dropped.
        with anyio.CancelScope(shield=True):
            await manager.disconnect(code, user_id, ws)
