"""FastAPI app: `uvicorn app.main:app --reload`."""

import asyncio
import logging
import time
import uuid
from typing import Any
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session

from .auth import verify_google_credential
from .config import Settings, get_settings
from .db import get_engine
from .game.cosmetics import usable_loadout
from .game.xp import XpStats, level_for, xp_for
from . import achievements
from .game.achievements import from_match
from .history import add_day
from .models import Match, MatchPlayer, Score, Throw, User
from .ratelimit import RateLimiter
from .rooms import Room, RoomManager
from .routes import router


def persistence_hooks(manager: RoomManager) -> None:
    """Record matches in Postgres when a room starts / ends a game."""

    async def on_start(room: Room) -> None:
        def write() -> str:
            with Session(get_engine()) as s:
                m = Match(room_code=room.code, mode=room.mode, difficulty=room.difficulty, race_seconds=room.race_seconds if room.mode == "race" else None)
                s.add(m)
                s.commit()
                return str(m.id)

        room.match_id = await asyncio.to_thread(write)

    async def on_finish(room: Room) -> None:
        if not room.match_id:
            return
        match_id = uuid.UUID(room.match_id)
        result = room.match.result if room.match else None
        # H-O-R-S-E is won on letters, not points, so it has no leaderboard.
        mode_key = None if not room.match or room.match.mode == "horse" else "shootout" if room.match.mode == "shootout" else f"race{room.race_seconds}"

        series_winner = room.series_winner if room.best_of > 1 else None

        def write() -> tuple[dict[str, tuple[int, int, Any]], dict[str, list[str]]]:
            """Returns each player's (XP gained, XP total, chosen loadout), and their new achievements."""
            awarded: dict[str, tuple[int, int, Any]] = {}
            unlocked: dict[str, list[str]] = {}
            with Session(get_engine()) as s:
                m = s.get(Match, match_id)
                if m:
                    m.ended_at = datetime.now(UTC)
                    if result and result["winner"]:
                        m.winner_id = uuid.UUID(result["winner"])
                    m.reason = result["reason"] if result else "aborted"
                    s.add(m)
                    # Everyone's line, for match history.
                    for row in result["standings"] if result else []:
                        if s.get(User, uuid.UUID(row["playerId"])) is None:
                            continue  # erased their data mid-match
                        s.add(
                            MatchPlayer(
                                match_id=match_id,
                                user_id=uuid.UUID(row["playerId"]),
                                rank=row["rank"],
                                won=row["playerId"] in result["winners"],
                                forfeited=row["forfeited"],
                                points=row["points"],
                                shots=row["shots"],
                                makes=row["makes"],
                                swishes=row["swishes"],
                                banks=row.get("banks", 0),
                                best_streak=row.get("best", 0),
                                letters=row.get("letters") if room.match and room.match.mode == "horse" else None,
                            )
                        )
                # Verified multiplayer scores feed the leaderboard (not for aborted games).
                if result and result["reason"] != "aborted" and mode_key:
                    for row in result["standings"]:
                        if not row["forfeited"]:
                            s.add(Score(user_id=uuid.UUID(row["playerId"]), mode=mode_key, difficulty=room.difficulty, score=row["points"]))
                # XP from the refereed match: everyone who finished, a bonus for the winner(s).
                if result and result["reason"] != "aborted":
                    for row in result["standings"]:
                        user = s.get(User, uuid.UUID(row["playerId"]))
                        if row["forfeited"] or user is None:
                            continue
                        won = row["playerId"] in result["winners"]
                        gained = xp_for(XpStats(row["shots"], row["makes"], row["swishes"], row.get("banks", 0), row.get("best", 0), matches=1, wins=int(won)))
                        user.xp += gained
                        s.add(user)
                        awarded[row["playerId"]] = (gained, user.xp, user.loadout)
                        # Achievements: the refereed line, and the history it adds to (its best streak)
                        add_day(s, user.id, "match", best_streak=row.get("best", 0))
                        mode = room.match.mode if room.match else room.mode
                        new = achievements.award(s, user.id, from_match(mode, row, won, series_won=series_winner == row["playerId"]))
                        new += achievements.check_totals(s, user.id)
                        if new:
                            unlocked[row["playerId"]] = sorted(set(new))
                s.commit()
            return awarded, unlocked

        awarded, unlocked = await asyncio.to_thread(write)
        for pid, (gained, total, loadout) in awarded.items():
            player = room.players.get(pid)
            if player is None:
                continue
            player.level = level_for(total)
            player.loadout = usable_loadout(loadout, player.level)  # a level-up may have unlocked their pick
            if player.ws is not None:
                await manager._send(player.ws, {"type": "xp", "gained": gained, "xp": total, "level": player.level})
                if pid in unlocked:
                    await manager._send(player.ws, {"type": "achievements", "ids": unlocked[pid]})

    async def on_throw(room: Room, user_id: str, payload: dict, sim, shot: dict) -> None:
        if not room.match_id:
            return
        match_id = uuid.UUID(room.match_id)
        r = sim.result

        def write() -> None:
            with Session(get_engine()) as s:
                uid = uuid.UUID(user_id)
                s.add(
                    Throw(
                        match_id=match_id,
                        user_id=uid,
                        power=float(payload["power"]),
                        angle=float(payload["angle"]),
                        arc=float(payload["arc"]),
                        aim=float(sim.launch.aim),
                        scored=r.scored,
                        swish=r.swish,
                        bank=r.bank,
                        miss=r.miss,
                        depth_error=float(r.error),
                        side_error=float(r.lateral),
                        green=bool(shot.get("green")),
                        points=shot["points"],
                    )
                )
                add_day(
                    s,
                    uid,
                    "match",
                    shots=1,
                    makes=int(r.scored),
                    swishes=int(r.scored and r.swish),
                    banks=int(r.scored and r.bank),
                    misses={r.miss: 1} if r.miss else None,
                )
                s.commit()

        await asyncio.to_thread(write)

    manager.on_start = on_start
    manager.on_finish = on_finish
    manager.on_throw = on_throw


def setup_monitoring(settings: Settings) -> None:
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if settings.sentry_dsn:
        import sentry_sdk  # optional dependency: pip install '.[monitoring]'

        # No PII: guest tokens never appear in events; IPs are not sent.
        sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment, traces_sample_rate=0.05, send_default_pii=False)


def create_app(settings: Settings | None = None, manager: RoomManager | None = None, limiter: RateLimiter | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_monitoring(settings)
    manager = manager or RoomManager(settings)
    persistence_hooks(manager)
    access = logging.getLogger("airhoops.http")

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
    # Swappable so tests don't call Google.
    app.state.verify_google = verify_google_credential
    app.state.limiter = limiter or RateLimiter(enabled=settings.rate_limit_enabled)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - start) * 1000
        if request.url.path != "/health":  # uptime pings would drown everything else
            level = logging.WARNING if response.status_code >= 500 or ms > 1000 else logging.INFO
            access.log(level, "%s %s %d %.0fms", request.method, request.url.path, response.status_code, ms)
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_origin_regex=settings.allowed_origin_regex or None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
