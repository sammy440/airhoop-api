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

Room and match timing (reconnect grace, heartbeats, shot clocks, buzzers) is tested with an injectable fake clock; the whole suite (incl. 1,440 physics-parity cases) runs in ~30 s.

## REST

| Method | Path | Auth | |
|---|---|---|---|
| GET | `/health` | | liveness (cheap; for load balancers) |
| GET | `/health/ready` | | readiness: DB answers (503 if not); point uptime monitors here |
| POST | `/auth/guest` | | `{nickname, avatar?}` → `{user, token}` · 10/min per IP |
| POST | `/auth/google` | | `{credential, guest_token?, nickname?, avatar?}` → `{user, token, account: new\|linked\|existing}` · 10/min per IP. Needs `GOOGLE_CLIENT_ID` (else 503) |
| GET / PATCH / DELETE | `/auth/me` | Bearer | who am I / update `{nickname?, avatar?}` / erase me + my scores and throws |
| POST | `/rooms` | Bearer | `{mode, difficulty, race_seconds, best_of: 1\|3\|5}` → room (you're the host) · 20/min |
| GET | `/rooms/{code}` | | room preview |
| POST | `/rooms/{code}/join` | Bearer | take a seat (409 if full / already playing) · 30/min |
| POST | `/quickmatch` | Bearer | `{difficulty: any\|rookie\|…}` → `{status: waiting, waiting, waited_s}` or `{status: matched, code}` · poll every ~2 s; you leave the queue after 10 s without a poll (`QUEUE_TIMEOUT_S`) · 60/min |
| DELETE | `/quickmatch` | Bearer | leave the queue |
| POST | `/scores` | Bearer | solo run `{mode, difficulty, score, shots, makes}` → `{best, rank}` · 30/min |
| GET | `/leaderboard?mode=timed60&difficulty=&period=day\|week\|all&limit=20` | | best per player, ties share a rank, with avatars |
| GET | `/leaderboard/me?mode=&difficulty=` | Bearer | your best, runs and rank for day / week / all |
| POST | `/telemetry/errors` | | browser crash report `{message, stack?, url?, release?}` · 20/min |

Solo scores are simulated in the browser, so they're self-reported, but the server rejects impossible runs:
more makes than shots, more points than `makes × max points per make` for the difficulty, a streak longer than
the makes, or more than one shot per second of a timed run.

Daily Challenge runs post with mode `daily20261030` (the UTC date). `app/game/daily.py` works out each day's
difficulty and clock exactly as the frontend's `daily.ts` does, so a daily run is also rejected for the wrong
difficulty, too many shots for that day's clock, or a date more than a day from today. Read a day's board with
`GET /leaderboard?mode=daily20261030`. Multiplayer scores are fully server-refereed.

## XP and levels

`app/game/xp.py` (mirrored by the frontend's `src/lib/game/xp.ts`): 1 XP a shot, 10 a make, +5 a swish or bank,
+5 per make in your best streak beyond the second, 25 a finished timed/streak/daily run, 50 for the day's daily
challenge (once), 20 a finished multiplayer game, 50 a win. Level 2 starts at 100 XP and each level needs 50 more
than the last (max level 50).

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/xp` | Bearer | Solo play since the last sync, as stats `{shots, makes, swishes, banks, best_streak, runs, daily?}` → `{xp, level, gained, capped}` · 30/min |

The client never sends an XP number: the server works it out from the stats (sanity-checked like scores) and caps
solo XP at 6,000 per UTC day. Multiplayer XP is awarded by the server at match end from the refereed stats and
sent to each player as `xp{gained, xp, level}`. `/auth/me` includes `xp` and `level`; room players include `level`.

Ball skin and trail: `PATCH /auth/me {loadout: {ball, trail}}` stores the player's pick; `/auth/me` and room players
carry `loadout` as others may see it, i.e. only what's unlocked at their level (`app/game/cosmetics.py`, the same
table as the frontend's `cosmetics.ts`); anything locked or unknown shows as the default.

## WebSocket `/ws/{code}?token=…`

Client → server: `ping{t}` · `ready{ready}` · `start` (host) · `end` (host) · `rematch` (host) · `settings{mode?, difficulty?, raceSeconds?, bestOf?}` (host, lobby only) · `react{reaction}` · `leave`
Server → client: `room_state{room, you}` · `presence{event: joined|left|disconnected|reconnected, player}` · `reaction{playerId, reaction}` · `xp{gained, xp, level}` (yours, at match end) · `pong{t}` · `error{code, message}`

Reactions are ids, never free text: `fire | clap | laugh | wow | flex | ball` (the client draws the emoji). Anyone
in the room can send them in any phase; they go to everyone, the sender included. At most 4 per player per 3 s;
extras are dropped quietly. An unknown id gets `error{code: "invalid_reaction"}`.

Lifecycle: `lobby ⇄ ready → in_progress → finished → lobby`. Quick-match rooms (`quick: true`) are a 60 s race whose
two seats start ready, so the first game starts by itself as soon as both players are connected; after that
they behave like any room (the longer-waiting player is host). `ready` is automatic when ≥2 players are
connected and all have readied up.

| Rule | Default | Env |
|---|---|---|
| Seat held after a disconnect | 30 s | `RECONNECT_GRACE_S` |
| Silent socket dropped (client pings every 10 s) | 25 s | `HEARTBEAT_TIMEOUT_S` |
| Sweeper interval (expired seats, empty rooms) | 5 s | `SWEEP_INTERVAL_S` |
| Players per room | 4 | `MAX_PLAYERS` |

Close codes: `4000` no heartbeat · `4001` opened in another tab · `4401` bad token · `4404` no such room · `4409` full/playing.

### Google sign-in

The frontend's "Continue with Google" button (Google Identity Services) sends Google's signed ID token to
`/auth/google`; the server verifies signature, audience (`GOOGLE_CLIENT_ID`) and expiry with `google-auth`, and
stores only the account ID (`users.google_sub`). No email, name or photo is kept.

- First sign-in on a device with a guest: that guest is linked (keeps look, scores, wins).
- Google account already linked: you get that player, on a new token for this device (`auth_tokens`), so
  signing in on a phone doesn't sign the laptop out.
- Otherwise a new player is created from the `nickname`/`avatar` sent along.

`DELETE /auth/me` also removes every device token.

### Series and lobby settings

Rooms play a single game or a best-of-3/5 series (`room.series = {best_of, wins, games, winner}` in every
`room_state`). Each finished game adds a win to its sole winner (shared wins and aborted games add none);
wins carry across `rematch` until someone reaches `best_of // 2 + 1`, and the rematch after that starts a
fresh series. The host can send `settings` in the lobby to change mode, difficulty, race length or series
length without a new room; that un-readies everyone and resets the series.

## Matches (Week 7)

Rooms are created with `{"mode": "shootout" | "race" | "horse", "difficulty", "race_seconds": 30–300}`.
When the host sends `start`, the server runs the match (`app/match.py`):

| | Shootout | Race | H-O-R-S-E |
|---|---|---|---|
| Who shoots | One player at a time, in join order | Everyone, freely | The setter, then each other player in order after them |
| Length | 5 shots each, then sudden death among tied leaders | `race_seconds` | Until one player is left (cap: 150 shots, then fewest letters wins) |
| Winner | Most makes | Most points (shots released before the buzzer count) | Last player without H-O-R-S-E |
| Clock | 20 s per turn (8 s if the shooter is disconnected); timeout = miss | — | As shootout |

**H-O-R-S-E:** the setter's make becomes the challenge by its kind: `swish` (touched nothing), `bank` (off the
board) or `bucket` (any other make). Every other player gets one try to match it: a swish needs a swish, a bank
a bank, a bucket any make. A failed match is a letter; five is out. The setter keeps setting until they miss,
then the next player sets. Points are still scored but don't decide anything, so H-O-R-S-E has no leaderboard.

Extra messages:

- Client → server: `throw{power, angle, arc, aim, t, camera?}` — the only per-shot data a client sends (`aim`: sideways throw angle in degrees, + = right; 0 if omitted; `camera`: thrown on camera, see the green rim below).
- Server → client: `match_start{mode, order, startAt, endsAt}`, `turn{playerId, shotsLeft, round, suddenDeath, setter, challenge, availableAt, deadline}`,
  `shot_result{playerId, scored, swish, points, multiplier, launch, wind, phase, path, events, duration}`,
  `scoreboard{scores}` (incl. `letters`), `forfeit{playerId}`, `match_end{winner, winners, standings, reason}`,
  and for H-O-R-S-E `challenge{playerId, challenge}` and `letter{playerId, letters, word, out}`. All carry `serverTime`.

**The green rim** (`app/game/green.py`, mirrored by the frontend's `green.ts`): a light loops round the rim every
3 s on the match clock, and the rim is green while it passes the front: 0.7 s a loop on Rookie, 0.55 Starter,
0.42 All-Star, 0.3 Legend (`Difficulty.green_window`). A throw with `camera: true` released while it's green is
replaced by `green_launch`: a fixed, ordered search for a launch that swishes in that wind with the hoop where it
will be. `shot_result` carries `green`. The parity fixtures pin `is_green` and `green_launch` to the browser's.

**Anti-cheat:** the server validates power/angle/arc ranges, enforces a 0.8 s cooldown and one ball in the air
per player, enforces turn order, chooses the wind, trusts the client's release time only within 0.5 s of its
own clock, then re-simulates the throw with `app/game/physics.py` (a line-for-line port of the browser's
physics, 3D: x toward the hoop, y up, z sideways) and scores it. `tests/test_physics_parity.py` checks 1,440 shots against fixtures exported from the
TypeScript code; regenerate with `GEN_PARITY=1 npx vitest run src/lib/game/parity.gen.test.ts` in the frontend.

**Dropped players:** a disconnected player keeps their seat for 30 s (turns still time out); after that, or on
`leave`, they forfeit. If one player remains they win by forfeit. Reconnecting mid-match replays a snapshot.

Finished matches store `winner_id`, every throw (`throws`), and each player's points in `scores` under
`shootout` / `race{seconds}` for the leaderboard (not H-O-R-S-E).

## Deploy

One container, one instance (rooms live in memory). `start.sh` runs `alembic upgrade head`, then Uvicorn with
`--proxy-headers` so rate limiting sees real client IPs.

**Railway** (config in `railway.json`: Dockerfile build, `/health` check, 1 replica, never sleeps)

1. New project → **Deploy from GitHub repo** → this repo.
2. In the project, **+ New → Database → PostgreSQL**.
3. On the API service → **Variables**:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (reference variable; uses Railway's private network)
   - `ALLOWED_ORIGINS` = `https://airhoops.vercel.app` (comma-separate extra domains, e.g. a custom domain).
     The production site and this project's Vercel branch/preview URLs are always allowed (`ALLOWED_ORIGIN_REGEX`).
   - `ENVIRONMENT` = `production` · optional `SENTRY_DSN`
4. **Settings → Networking → Generate Domain**. That `https://…up.railway.app` URL is the frontend's
   `NEXT_PUBLIC_API_URL`; WebSockets work on the same domain (`wss://`).

Railway sets `PORT`; `start.sh` uses it. Migrations run on every deploy before the server starts.
Keep it at 1 replica (rooms are in memory) and leave serverless/app sleeping off, or live matches get dropped.

`DATABASE_URL` may be given as `postgres://…` (as managed hosts print it); it's converted to the psycopg driver.

## Monitoring

- **Uptime:** point a free monitor (Better Stack, UptimeRobot) at `GET /health/ready` every minute.
- **Errors:** set `SENTRY_DSN` to send backend exceptions to Sentry (installed via the `monitoring` extra; no PII).
  Browser crashes arrive at `/telemetry/errors` and are logged at ERROR level (and so reach Sentry too).
- **Logs:** one line per request (`METHOD path status ms`); 5xx and requests slower than 1 s log as WARNING.
- **Rate limits:** in-memory sliding window per IP (see table). Swap for Redis when running several instances.

## Notes

- Guest tokens are random 256-bit values; only their SHA-256 hash is stored.
- Rooms live in memory on one instance (README: single instance first, Redis pub/sub when scaling out).
- `matches` rows are written when a host starts a game and closed (with the winner) when it ends.
