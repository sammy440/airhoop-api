"""FastAPI app: `uvicorn app.main:app --reload`."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from .config import Settings, get_settings
from .db import get_engine
from .models import Match, Score, Throw
from .rooms import Room, RoomManager
from .routes import router


def persistence_hooks(manager: RoomManager) -> None:
    """Record matches in Postgres when a room starts / ends a game."""

    async def on_start(room: Room) -> None:
        def write() -> str:
            with Session(get_engine()) as s:
                m = Match(room_code=room.code, mode=room.mode, difficulty=room.difficulty)
                s.add(m)
                s.commit()
                return str(m.id)

        room.match_id = await asyncio.to_thread(write)

    async def on_finish(room: Room) -> None:
        if not room.match_id:
            return
        match_id = uuid.UUID(room.match_id)
        result = room.match.result if room.match else None
        mode_key = None if not room.match else "shootout" if room.match.mode == "shootout" else f"race{room.race_seconds}"

        def write() -> None:
            with Session(get_engine()) as s:
                m = s.get(Match, match_id)
                if m:
                    m.ended_at = datetime.now(UTC)
                    if result and result["winner"]:
                        m.winner_id = uuid.UUID(result["winner"])
                    s.add(m)
                # Verified multiplayer scores feed the leaderboard (not for aborted games).
                if result and result["reason"] != "aborted" and mode_key:
                    for row in result["standings"]:
                        if not row["forfeited"]:
                            s.add(Score(user_id=uuid.UUID(row["playerId"]), mode=mode_key, difficulty=room.difficulty, score=row["points"]))
                s.commit()

        await asyncio.to_thread(write)

    async def on_throw(room: Room, user_id: str, payload: dict, sim, points: int) -> None:
        if not room.match_id:
            return
        match_id = uuid.UUID(room.match_id)

        def write() -> None:
            with Session(get_engine()) as s:
                s.add(
                    Throw(
                        match_id=match_id,
                        user_id=uuid.UUID(user_id),
                        power=float(payload["power"]),
                        angle=float(payload["angle"]),
                        arc=float(payload["arc"]),
                        scored=sim.result.scored,
                        points=points,
                    )
                )
                s.commit()

        await asyncio.to_thread(write)

    manager.on_start = on_start
    manager.on_finish = on_finish
    manager.on_throw = on_throw


def create_app(settings: Settings | None = None, manager: RoomManager | None = None) -> FastAPI:
    settings = settings or get_settings()
    manager = manager or RoomManager(settings)
    persistence_hooks(manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if settings.sweep_interval_s > 0:
            tasks.append(asyncio.create_task(manager.run_sweeper()))
        if settings.tick_interval_s > 0:
            tasks.append(asyncio.create_task(manager.run_ticker()))
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(title="AirHoops API", version="0.1.0", lifespan=lifespan)
    app.state.rooms = manager
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
