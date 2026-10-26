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

Room and match timing (reconnect grace, heartbeats, shot clocks, buzzers) is tested with an injectable fake clock; the whole suite (incl. 480 physics-parity cases) runs in ~9 s.

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

## Matches (Week 7)

Rooms are created with `{"mode": "shootout" | "race", "difficulty", "race_seconds": 30–300}`.
When the host sends `start`, the server runs the match (`app/match.py`):

| | Shootout | Race |
|---|---|---|
| Who shoots | One player at a time, in join order | Everyone, freely |
| Length | 5 shots each, then sudden death among tied leaders | `race_seconds` |
| Winner | Most makes | Most points (shots released before the buzzer count) |
| Clock | 20 s per turn (8 s if the shooter is disconnected); timeout = miss | — |

Extra messages:

- Client → server: `throw{power, angle, arc, t}` — the only per-shot data a client sends.
- Server → client: `match_start{mode, order, startAt, endsAt}`, `turn{playerId, shotsLeft, round, suddenDeath, availableAt, deadline}`,
  `shot_result{playerId, scored, swish, points, multiplier, launch, wind, phase, path, events, duration}`,
  `scoreboard{scores}`, `forfeit{playerId}`, `match_end{winner, winners, standings, reason}`. All carry `serverTime`.

**Anti-cheat:** the server validates power/angle/arc ranges, enforces a 0.8 s cooldown and one ball in the air
per player, enforces turn order, chooses the wind, trusts the client's release time only within 0.5 s of its
own clock, then re-simulates the throw with `app/game/physics.py` (a line-for-line port of the browser's
physics) and scores it. `tests/test_physics_parity.py` checks 480 shots against fixtures exported from the
TypeScript code; regenerate with `GEN_PARITY=1 npx vitest run src/lib/game/parity.gen.test.ts` in the frontend.

**Dropped players:** a disconnected player keeps their seat for 30 s (turns still time out); after that, or on
`leave`, they forfeit. If one player remains they win by forfeit. Reconnecting mid-match replays a snapshot.

Finished matches store `winner_id`, every throw (`throws`), and each player's points in `scores` under
`shootout` / `race{seconds}` for the leaderboard.

## Notes

- Guest tokens are random 256-bit values; only their SHA-256 hash is stored.
- Rooms live in memory on one instance (README: single instance first, Redis pub/sub when scaling out).
- `matches` rows are written when a host starts a game and closed (with the winner) when it ends.
