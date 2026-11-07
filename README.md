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
| POST | `/invites` | Bearer | `{to, code}`: invite someone **you've played a match with** (else 403 `not_an_opponent`) to your room, while it's in the lobby with a seat free (not quick match). One live invite per sender; kept in memory, like rooms · 20/min |
| GET | `/invites` | Bearer | invites you can still take up, newest first, with the room's game: dropped once the room starts, fills, goes, its sender leaves, after 10 min, or once you join · poll ~10 s · 30/min |
| DELETE | `/invites/{from_id}` | Bearer | dismiss one |
| POST | `/scores` | Bearer | solo run `{mode, difficulty, score, shots, makes}` → `{best, rank}` · 30/min |
| GET | `/leaderboard?mode=timed60&difficulty=&period=day\|week\|all&limit=20` | | best per player, ties share a rank, with avatars |
| GET | `/leaderboard/me?mode=&difficulty=` | Bearer | your best, runs and rank for day / week / all |
| GET | `/me/matches?limit=20&before=` | Bearer | your finished multiplayer matches, newest first, with everyone's line (rank, points, makes, swishes, letters); page with `before` = the last one's `started_at` · 60/min |
| GET | `/me/stats?days=30` | Bearer | your shooting over the last 1–365 UTC days: totals, solo vs match, per day, misses by kind, where refereed misses land (`miss_bias`), and your all-time record by mode · 60/min |
| GET | `/me/achievements` | Bearer | what you've earned `[{id, unlocked_at}]`, oldest first · 60/min |
| POST | `/me/achievements/{id}` | Bearer | claim a solo moment only the browser sees: `buzzer_beater`, `green_light`, `world_tour` (anything else: 422 `not_claimable`) → `{id, unlocked_at, new}` · 30/min |
| GET | `/me/bests` | Bearer | your personal best per board, keyed like the browser's bests (`timed60:rookie`, `streak:legend`, `free:starter`, `daily20261106`; dailies from the last 2 days only) with `has_ghost` · 60/min |
| GET | `/me/bests/{key}/ghost` | Bearer | that best's ghost: its launches `[{t, speed, angle, aim, wind, phase, points}]` (404 if none) · 60/min |
| PUT | `/me/bests/{key}` | Bearer | `{points, makes, streak, at, ghost?}` → keeps whichever is better, this or the stored one (`updated` says which). Streak boards compare the streak, the rest points. Checked like `/scores`; a ghost (timed/daily only, ≤ 1 shot a second, none after the buzzer) must add up to the run's points and makes · 30/min |
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

`/xp` may also carry `misses: {short, long, left, right, rim, airball}` (counts, at most the missed shots). They earn
nothing; with the other counts they're added to the player's day in `player_days` (`app/history.py`), which is what
`/me/stats` reads. Refereed multiplayer throws are added there too, as they're decided, under `source: match`.

The client never sends an XP number: the server works it out from the stats (sanity-checked like scores) and caps
solo XP at 6,000 per UTC day. Multiplayer XP is awarded by the server at match end from the refereed stats and
sent to each player as `xp{gained, xp, level}`. `/auth/me` includes `xp` and `level`; room players include `level`.

### Achievements

`app/game/achievements.py` (mirrored by the frontend's `achievements.ts`; both test suites pin the ids). Most are the
server's call: **totals** from the player's history in `player_days` (makes, swishes, banks, shots, best streak,
dailies finished, most dailies in a row), checked on every `/xp` sync and at match end, and **match** ones from the
refereed line at the end of a match that wasn't aborted (`first_win`, `horse_clean`: win H-O-R-S-E with no letter,
`shootout_perfect`: win a shootout making every shot, at least 5, `series_win`). New ones come back in `/xp`'s
`achievements` and, after a match, as `achievements{ids}` (after `xp`). Three solo moments are claimed by the
browser and so self-reported, like solo scores.

Ball skin and trail: `PATCH /auth/me {loadout: {ball, trail}}` stores the player's pick; `/auth/me` and room players
carry `loadout` as others may see it, i.e. only what's unlocked at their level (`app/game/cosmetics.py`, the same
table as the frontend's `cosmetics.ts`); anything locked or unknown shows as the default.

## WebSocket `/ws/{code}?token=…`

Client → server: `ping{t}` · `ready{ready}` · `start` (host) · `end` (host) · `rematch` (host) · `settings{mode?, difficulty?, raceSeconds?, bestOf?}` (host, lobby only) · `react{reaction}` · `chat{text | quick}` · `leave`
Server → client: `room_state{room, you}` · `presence{event: joined|left|disconnected|reconnected, player}` · `reaction{playerId, reaction}` · `xp{gained, xp, level}` (yours, at match end) · `achievements{ids}` (yours, new, after `xp`) · `pong{t}` · `error{code, message}`

Chat: `chat{text}` or `chat{quick}` → `chat{playerId, text | quick}` to everyone in the room (`app/chat.py`). **Never
stored**: relayed and forgotten (no table, no log line, no history kept for reconnects). Rooms joined by code allow
free text (cleaned: control characters out, links → [link], emails → [hidden], profanity masked, 200 characters);
quick-match rooms allow only the quick-chat ids `gg | nice_shot | rematch | one_more | good_luck | unlucky | wow |
thanks` (`error{code: "quick_chat_only"}` otherwise). 5 messages per player per 10 s (`chat_too_fast`).

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

**Watching:** `/ws/{code}?token=…&watch=1` takes no seat and gets everything the players get (`room_state` with
`you: null, spectating: true`, the match from a snapshot if it's already running, reactions and chat), but can only
`ping` and `leave`; anything else is `error{code: "spectating"}`. Up to 20 watchers a room (`watchers_full`); quick
matches, between strangers, can't be watched (`not_watchable`, close `4403`); a player can't watch their own room.
`room.spectators` counts them. Watchers are closed with `4404` when the room goes.

Close codes: `4000` no heartbeat · `4001` opened in another tab · `4401` bad token · `4404` no such room · `4409` full/playing.

### Skill rating

`app/game/rating.py`: Elo from refereed results, updated at the end of every multiplayer match that wasn't aborted
(friends' rooms too). A 3–4 player match counts as every pair in it, by rank (beat, tie, lose), averaged; forfeits
rank last. K is 40 for a player's first 20 rated matches (`provisional` in `/auth/me`), then 24; nobody drops below
100. Everyone starts at 1000. After a match each player gets `rating{rating, change}`, and `/me/matches` shows each
line's `rating_change`.

Quick match pairs the longest-waiting compatible player **within the rating window**: 150 points apart at once,
40 more for each second the longer-waiting of the two has waited, and anyone after 20 s, so nobody waits forever.

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
3 s on the match clock, and the rim is green while it passes the front: 1.2 s a loop on Rookie, 1.0 Starter,
0.8 All-Star, 0.6 Legend (`Difficulty.green_window`). A throw with `camera: true` released while it's green is
replaced by `green_launch`: a fixed, ordered search for a launch that swishes in that wind with the hoop where it
will be. `shot_result` carries `green`. The parity fixtures pin `is_green` and `green_launch` to the browser's.

**Anti-cheat:** the server validates power/angle/arc ranges, enforces a 0.8 s cooldown and one ball in the air
per player, enforces turn order, chooses the wind, trusts the client's release time only within 0.5 s of its
own clock, then re-simulates the throw with `app/game/physics.py` (a line-for-line port of the browser's
physics, 3D: x toward the hoop, y up, z sideways) and scores it. `tests/test_physics_parity.py` checks 1,440 shots against fixtures exported from the
TypeScript code; regenerate with `GEN_PARITY=1 npx vitest run src/lib/game/parity.gen.test.ts` in the frontend.

**Dropped players:** a disconnected player keeps their seat for 30 s (turns still time out); after that, or on
`leave`, they forfeit. If one player remains they win by forfeit. Reconnecting mid-match replays a snapshot.

Finished matches store `winner_id` and how it ended (`reason`), every throw (`throws`, with the server's verdict:
swish, bank, miss kind, aim, green, and where it came down: `depth_error` + = long, `side_error` + = right), each
player's line in `match_players` (for `/me/matches`), and each player's points in `scores` under
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
