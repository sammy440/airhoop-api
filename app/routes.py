"""REST and WebSocket endpoints."""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from sqlalchemy import delete, func, text, update
from sqlmodel import Session, select

from .auth import CurrentUser, GoogleTokenError, SessionDep, create_guest, issue_token, user_from_token
from .db import get_engine
from .models import AuthToken, Match, Score, Throw, User
from .ratelimit import client_ip, rate_limit
from .rooms import RoomError, RoomManager
from .schemas import (
    ClientError,
    GoogleOut,
    GoogleSignIn,
    GuestCreate,
    GuestOut,
    LeaderboardEntry,
    PersonalBest,
    PersonalBests,
    ProfileUpdate,
    QuickMatchOut,
    QuickMatchRequest,
    RoomCreate,
    RoomOut,
    ScoreCreate,
    ScoreSaved,
    UserOut,
)

log = logging.getLogger("airhoops")
router = APIRouter()


def manager_of(request: Request | WebSocket) -> RoomManager:
    return request.app.state.rooms


def room_error(e: RoomError) -> HTTPException:
    return HTTPException(e.status, {"code": e.code, "message": e.message})


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up (cheap; for load balancers)."""
    return {"status": "ok"}


@router.get("/health/ready")
def ready(request: Request, response: Response) -> dict[str, object]:
    """Readiness: the database answers. Point the uptime monitor here."""
    try:
        with Session(get_engine()) as s:
            s.exec(text("SELECT 1"))
        db = "ok"
    except Exception:  # noqa: BLE001 - any DB failure means not ready
        log.exception("readiness check failed")
        db = "down"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if db == "ok" else "degraded", "db": db, "rooms": len(manager_of(request).rooms)}


# ── Guest auth ──


def user_out(user: User) -> UserOut:
    return UserOut(id=user.id, nickname=user.nickname, avatar=user.avatar, google=user.google_sub is not None)


@router.post("/auth/guest", status_code=status.HTTP_201_CREATED, dependencies=[Depends(rate_limit("guest", 10, 60))])
def guest(body: GuestCreate, session: SessionDep) -> GuestOut:
    user, token = create_guest(session, body.nickname, body.avatar.model_dump() if body.avatar else None)
    return GuestOut(user=user_out(user), token=token)


@router.post("/auth/google", dependencies=[Depends(rate_limit("google", 10, 60))])
def google_sign_in(body: GoogleSignIn, session: SessionDep, request: Request) -> GoogleOut:
    """
    Sign in with a Google ID token. Only Google's account ID is stored (no email, name or photo).
    Already linked → that player, on a new token for this device. Otherwise this device's
    guest is linked (keeping its look and scores), or a new player is created.
    """
    client_id = request.app.state.settings.google_client_id
    if not client_id:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"code": "google_disabled", "message": "Google sign-in isn't set up on this server"})
    try:
        sub = request.app.state.verify_google(body.credential, client_id)
    except GoogleTokenError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"code": "bad_google_token", "message": "Google sign-in failed. Try again."}) from e

    user = session.exec(select(User).where(User.google_sub == sub)).first()
    account = "existing"
    if user is None:
        guest = user_from_token(session, body.guest_token)
        if guest is not None and guest.google_sub is None:
            user, account = guest, "linked"
        else:
            # Unusable placeholder hash: this player only signs in through auth_tokens.
            nickname = body.nickname or f"Player {secrets.randbelow(9000) + 1000}"
            user = User(nickname=nickname, token_hash=secrets.token_hex(32), avatar=body.avatar.model_dump() if body.avatar else None)
            account = "new"
        user.google_sub = sub
        session.add(user)
        session.flush()
    token = issue_token(session, user)
    session.commit()
    session.refresh(user)
    return GoogleOut(user=user_out(user), token=token, account=account)


@router.get("/auth/me")
def me(user: CurrentUser) -> UserOut:
    return user_out(user)


@router.patch("/auth/me", dependencies=[Depends(rate_limit("profile", 30, 60))])
def update_profile(body: ProfileUpdate, user: CurrentUser, session: SessionDep, request: Request) -> UserOut:
    if body.nickname is not None:
        user.nickname = body.nickname
    if body.avatar is not None:
        user.avatar = body.avatar.model_dump()
    session.add(user)
    session.commit()
    # Live rooms show the new name/look right away.
    anyio.from_thread.run(manager_of(request).update_player, str(user.id), user.nickname, user.avatar)
    return user_out(user)


@router.delete("/auth/me", status_code=status.HTTP_204_NO_CONTENT)
def delete_me(user: CurrentUser, session: SessionDep) -> None:
    """Erase this guest and everything recorded about them (scores, throws, match wins)."""
    session.exec(delete(AuthToken).where(AuthToken.user_id == user.id))
    session.exec(delete(Throw).where(Throw.user_id == user.id))
    session.exec(delete(Score).where(Score.user_id == user.id))
    session.exec(update(Match).where(Match.winner_id == user.id).values(winner_id=None))
    session.delete(user)
    session.commit()


# ── Rooms ──


@router.post("/rooms", status_code=status.HTTP_201_CREATED, dependencies=[Depends(rate_limit("rooms", 20, 60))])
def create_room(body: RoomCreate, user: CurrentUser, request: Request) -> RoomOut:
    room = manager_of(request).create(str(user.id), user.nickname, body.mode, body.difficulty, body.race_seconds, avatar=user.avatar, best_of=body.best_of)
    return RoomOut(**room.public())


@router.get("/rooms/{code}")
def get_room(code: str, request: Request) -> RoomOut:
    try:
        return RoomOut(**manager_of(request).get(code).public())
    except RoomError as e:
        raise room_error(e) from e


@router.post("/rooms/{code}/join", dependencies=[Depends(rate_limit("join", 30, 60))])
def join_room(code: str, user: CurrentUser, request: Request) -> RoomOut:
    try:
        return RoomOut(**manager_of(request).join(code, str(user.id), user.nickname, avatar=user.avatar).public())
    except RoomError as e:
        raise room_error(e) from e


# ── Quick match ──


@router.post("/quickmatch", dependencies=[Depends(rate_limit("quickmatch", 60, 60))])
def quick_match(body: QuickMatchRequest, user: CurrentUser, request: Request) -> QuickMatchOut:
    """Poll every ~2 s while looking for a game: you stay queued only while you keep polling."""
    return QuickMatchOut(**manager_of(request).quick_match(str(user.id), user.nickname, body.difficulty, avatar=user.avatar))


@router.delete("/quickmatch", status_code=status.HTTP_204_NO_CONTENT)
def cancel_quick_match(user: CurrentUser, request: Request) -> None:
    manager_of(request).leave_queue(str(user.id))


# ── Scores / leaderboard ──

PERIODS = {"day": timedelta(days=1), "week": timedelta(days=7), "all": None}
ModeKey = Annotated[str, Query(pattern=r"^(free|streak|timed\d{2,4}|daily\d{8}|shootout|race\d{2,3})$")]
DifficultyFilter = Annotated[str | None, Query(pattern=r"^(rookie|starter|allstar|legend)$")]


def _bests(mode: str, difficulty: str | None, period: str):
    """Subquery: each player's best score and run count on one board."""
    q = select(Score.user_id, func.max(Score.score).label("best"), func.count(Score.id).label("games")).where(Score.mode == mode)
    if difficulty:
        q = q.where(Score.difficulty == difficulty)
    if PERIODS[period]:
        q = q.where(Score.created_at >= datetime.now(UTC) - PERIODS[period])
    return q.group_by(Score.user_id).subquery()


def _personal(session: Session, user_id, mode: str, difficulty: str | None, period: str) -> PersonalBest | None:
    bests = _bests(mode, difficulty, period)
    mine = session.exec(select(bests.c.best, bests.c.games).where(bests.c.user_id == user_id)).first()
    if mine is None:
        return None
    above = session.exec(select(func.count()).select_from(bests).where(bests.c.best > mine.best)).one()
    return PersonalBest(best=mine.best, games=mine.games, rank=above + 1)


@router.post("/scores", status_code=status.HTTP_201_CREATED, dependencies=[Depends(rate_limit("scores", 30, 60))])
def submit_score(body: ScoreCreate, user: CurrentUser, session: SessionDep) -> ScoreSaved:
    # Solo runs are simulated in the browser, so they're self-reported but sanity-checked
    # (ScoreCreate.plausible). Multiplayer scores are server-verified (main.persistence_hooks).
    session.add(Score(user_id=user.id, mode=body.mode, difficulty=body.difficulty, score=body.score, shots=body.shots, makes=body.makes))
    session.commit()
    standing = _personal(session, user.id, body.mode, body.difficulty, "all")
    assert standing is not None
    return ScoreSaved(best=standing.best, rank=standing.rank)


@router.get("/leaderboard")
def leaderboard(
    session: SessionDep,
    mode: ModeKey = "timed60",
    difficulty: DifficultyFilter = None,
    period: Literal["day", "week", "all"] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[LeaderboardEntry]:
    bests = _bests(mode, difficulty, period)
    q = (
        select(User.id, User.nickname, User.avatar, bests.c.best, bests.c.games)
        .join(bests, bests.c.user_id == User.id)
        .order_by(bests.c.best.desc(), bests.c.games.asc(), User.nickname)
        .limit(limit)
    )
    entries: list[LeaderboardEntry] = []
    for row in session.exec(q).all():
        # Ties share a rank (1, 2, 2, 4).
        rank = entries[-1].rank if entries and entries[-1].best == row.best else len(entries) + 1
        entries.append(LeaderboardEntry(rank=rank, user_id=row.id, nickname=row.nickname, avatar=row.avatar, best=row.best, games=row.games))
    return entries


@router.get("/leaderboard/me")
def my_bests(user: CurrentUser, session: SessionDep, mode: ModeKey = "timed60", difficulty: DifficultyFilter = None) -> PersonalBests:
    return PersonalBests(**{p: _personal(session, user.id, mode, difficulty, p) for p in PERIODS})


# ── Monitoring ──


@router.post("/telemetry/errors", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(rate_limit("errors", 20, 60))])
def client_error(body: ClientError, request: Request) -> dict[str, str]:
    """Browser crash reports land in the API logs (and Sentry, if configured)."""
    log.error("client error: %s", body.message, extra={"client_error": body.model_dump(), "ip": client_ip(request)})
    return {"status": "accepted"}


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
        identity = (str(user.id), user.nickname, user.avatar) if user else None
    if identity is None:
        await ws.send_json({"type": "error", "code": "unauthorized", "message": "Missing or invalid guest token"})
        await ws.close(code=4401)
        return
    user_id, nickname, avatar = identity
    try:
        await manager.connect(code, user_id, nickname, ws, avatar=avatar)
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
