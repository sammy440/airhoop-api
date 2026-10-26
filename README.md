# AirHoops — Backend

FastAPI · SQLModel · Alembic · PostgreSQL · WebSockets

Guest sessions, rooms with live presence, match records, scores and leaderboards.
Only small JSON travels over the network: never video, never landmarks.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env                 # defaults: local Postgres "airhoops", CORS for :3000
createdb airhoops                    # once
.venv/bin/alembic upgrade head       # create / migrate tables
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Interactive API docs: http://localhost:8000/docs

## Tests

```bash
createdb airhoops_test   # once
.venv/bin/pytest
```

Room timing (reconnect grace, heartbeats) is tested with an injectable fake clock, so the suite runs in ~1.5 s.

## REST

| Method | Path | Auth | |
|---|---|---|---|
| GET | `/health` | | liveness |
| POST | `/auth/guest` | | `{nickname}` → `{user, token}` |
| GET / PATCH | `/auth/me` | Bearer | who am I / rename |
| POST | `/rooms` | Bearer | `{mode, difficulty}` → room (you're the host) |
| GET | `/rooms/{code}` | | room preview |
| POST | `/rooms/{code}/join` | Bearer | take a seat (409 if full / already playing) |
| POST | `/scores` | Bearer | `{mode, difficulty, score}` (solo, self-reported) |
| GET | `/leaderboard?mode=timed60&difficulty=&period=day\|week\|all&limit=20` | | best per player |

## WebSocket `/ws/{code}?token=…`

Client → server: `ping{t}` · `ready{ready}` · `start` (host) · `end` (host) · `rematch` (host) · `leave`
Server → client: `room_state{room, you}` · `presence{event: joined|left|disconnected|reconnected, player}` · `pong{t}` · `error{code, message}`

Lifecycle: `lobby ⇄ ready → in_progress → finished → lobby`. `ready` is automatic when ≥2 players are
connected and all have readied up.

| Rule | Default | Env |
|---|---|---|
| Seat held after a disconnect | 30 s | `RECONNECT_GRACE_S` |
| Silent socket dropped (client pings every 10 s) | 25 s | `HEARTBEAT_TIMEOUT_S` |
| Sweeper interval (expired seats, empty rooms) | 5 s | `SWEEP_INTERVAL_S` |
| Players per room | 4 | `MAX_PLAYERS` |

Close codes: `4000` no heartbeat · `4001` opened in another tab · `4401` bad token · `4404` no such room · `4409` full/playing.

## Notes

- Guest tokens are random 256-bit values; only their SHA-256 hash is stored.
- Rooms live in memory on one instance (README: single instance first, Redis pub/sub when scaling out).
- `matches` rows are written when a host starts a game and closed when it ends; `throws` and verified
  multiplayer scores arrive in Week 7 (server-side re-simulation).
