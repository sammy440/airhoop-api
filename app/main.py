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
from .models import Match
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

        def write() -> None:
            with Session(get_engine()) as s:
                m = s.get(Match, match_id)
                if m:
                    m.ended_at = datetime.now(UTC)
                    s.add(m)
                    s.commit()

        await asyncio.to_thread(write)

    manager.on_start = on_start
    manager.on_finish = on_finish


def create_app(settings: Settings | None = None, manager: RoomManager | None = None) -> FastAPI:
    settings = settings or get_settings()
    manager = manager or RoomManager(settings)
    persistence_hooks(manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(manager.run_sweeper()) if settings.sweep_interval_s > 0 else None
        yield
        if task:
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
