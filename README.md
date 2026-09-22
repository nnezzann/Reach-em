# Reach'em

Reach'em is a Slack bot that helps a requester reach the smallest useful audience when a teammate is unavailable. Instead of posting in a shared channel and interrupting dozens of people, the bot lets requesters select specific people for direct messages or deliberately choose a public-channel/workspace broadcast.

## Features

- **Manual recipient selection**: Requesters pick specific people to reach via a multi-user picker
- **Broadcast scopes**: Option to reach everyone in selected public channels or across all public workspace channels
- **Two-stage reach flow**: `/reach` opens a target picker, then updates the same modal with recipients, scope, message, and required retention duration; channel broadcasts add a final channel-selection stage
- **Per-responder acknowledgment**: DM respondents see an in-place thank-you edit; broadcast respondents get an ephemeral (only-visible-to-them) acknowledgment
- **Three response types**: "I know", "I don't know", and "Custom message" for easy feedback
- **Message retention**: automatically delete each DM or broadcast after a required number of minutes, hours, or days
- **Broadcast cleanup**: once a broadcast reaches its response threshold, the channel post is deleted and the status is shown only ephemerally
- **Two independent closure pools**: manual DM messages close via a hunt-wide 3-"I know" threshold; each broadcast message closes via its own local thresholds (3 "I know" OR 5 total responses, whichever first) — the two counter systems are fully decoupled
- **Bot-relay delivery**: Messages are sent by Reach and identify the requester and target; send-as-user OAuth is not implemented
- **Privacy boundaries**: The bot does not read private DMs or expose affinity scores; public-channel broadcasting is explicit and requester-selected

## Slack App Setup

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click "Create New App"
2. Choose "From scratch" and give your app a name (e.g., "Reach'em")
3. Select your workspace

### 2. Configure Basic Information

- Add a display name and description
- Add an app icon (optional but recommended)

### 3. Add Bot Permissions

Navigate to **Bot Permissions** and add the following scopes:

- `app_mentions:read` - App mention event support
- `channels:history` - Public-channel message/event access
- `channels:read` - Public-channel discovery
- `chat:write` - Send and delete Reach messages
- `commands` - `/reach` and `/reach-cleanup` slash commands
- `groups:history` - Private-channel event support
- `im:history` and `im:write` - Direct-message delivery and event support
- `mpim:history` - Group-DM event support
- `users:read` - Resolve target profiles and display names

### 4. Enable Socket Mode

Navigate to **Socket Mode** and enable it. This is required for local development and simplifies deployment by avoiding the need for a public HTTP endpoint.

### 5. Install the App to Your Workspace

Navigate to **Install App** and click "Install to Workspace". This authorizes the bot with the permissions you configured.

### 6. Copy Tokens

After installation, you'll need two tokens:

- **Bot User OAuth Token**: Starts with `xoxb-...` (found in OAuth & Permissions → Bot User OAuth Token)
- **App-Level Token**: Starts with `xapp-...` (found in Basic Information → App-Level Token → Create Token)

Copy both tokens to your `.env` file as `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`.

### 7. Apply Manifest

The included `manifest.json` file contains the required configuration. Apply it to your Slack app by navigating to the app's settings and importing the manifest, or manually verify that the settings match.

## Development

### Prerequisites

- Python 3.12+
- `uv` package manager
- PostgreSQL 15+ (optional, falls back to in-memory storage)
- Redis (optional, for presence caching)

### Setup

1. Clone the repository and navigate to the project directory
2. Install dependencies:

```bash
uv sync --extra dev
```

3. Copy the example environment file:

```bash
cp .env.example .env
```

4. Edit `.env` and add your Slack tokens:

```
SLACK_BOT_TOKEN=xoxb-your-bot-token-here
SLACK_APP_TOKEN=xapp-your-app-token-here
```

5. Configure PostgreSQL for persistent production storage. Redis remains optional and is currently only used by dormant presence-cache code:

```
DATABASE_URL=postgresql://user:password@localhost/reachem
REDIS_URL=redis://localhost:6379
```

If `DATABASE_URL` is omitted, the app falls back to an in-memory repository. That is useful for local experiments, but all tracked requests, pings, response counters, and retention state disappear when the process restarts.

### Running the Bot

Run the app (Socket Mode handler + health server, in one process):

```bash
uv run python app.py
```

The bot connects to Slack via Socket Mode and responds to `/reach` and
`/reach-cleanup confirm`. The health server listens on `0.0.0.0:$PORT` (default 8000)
and serves only `GET /health`; it carries no Slack traffic. Each health request is logged
at `INFO` level as `GET /health` and exists for Render's health check and an external
keep-alive monitor (see [Deployment](#deployment)).

To remove messages sent before retention support was introduced, run
`/reach-cleanup confirm`. This only deletes messages tracked as Reach bot messages;
it does not search for or remove unrelated workspace messages.

### Testing

Run the test suite:

```bash
uv run pytest
```

Run linting and type checking:

```bash
uv run ruff check .
uv run mypy src
```

## Architecture

The bot uses a simple architecture with clear separation of concerns:

- **Handlers**: Slack interaction handlers for commands, views, and actions
- **Persistence**: PostgreSQL and in-memory storage for reach requests and pings
- **Rendering**: Slack Block Kit builders for modals and messages
- **Broadcast**: Background fan-out for channel/workspace broadcast scopes

The ranking/suggestion machinery is currently dormant; the bot uses manual recipient selection only. The ranking modules (`reach_bot.ranking`, `reach_bot.affinity`, `reach_bot.slack_provider`) are preserved for possible future use.

### Current reach flow

1. `/reach` opens Stage 1 with a target user picker.
2. Submitting Stage 1 updates the same modal to Stage 2. The requester selects hand-picked recipients, chooses no broadcast, a channel broadcast, or a workspace broadcast, edits the message, and supplies a required retention duration in minutes, hours, or days.
3. A channel broadcast pushes a final channel-selection view. Workspace broadcasts resolve all non-archived public channels in the background.
4. Hand-picked recipients receive synchronous bot-relay DMs. Broadcast fan-out runs as a background task and is deduplicated by the stored ping records.
5. Recipients can choose `I know`, `I don't know`, or `Custom message`.

Manual DMs and broadcasts use separate response pools. Manual DMs use a request-wide limit of three `I know` responses. Each broadcast message closes independently at three `I know` responses or five total responses, whichever comes first. Broadcast responses never affect the DM counter.

## Privacy & Security

- No tokens or message contents are logged
- Public-channel posts happen only when the requester explicitly chooses a broadcast scope
- The bot does not read private DMs; workspace broadcast resolution uses public channels only
- All suggestions are ephemeral (no persistent ranking displayed)
- Affinity scores are kept internal to ranking and never exposed as leaderboards

## Deployment

The bot is deployed as a **Render free-tier Web Service** that talks to Slack entirely
over **Socket Mode**. HTTP mode / Slack Request URLs are not used: there is no webhook
endpoint and no `SLACK_SIGNING_SECRET` in this deployment.

### Why a Web Service, not a Background Worker

Render's free plan does not offer Background Workers, so the long-lived Socket Mode
process is deployed as `type: web`. A Web Service has to bind `$PORT` and answer HTTP
requests to stay up, which is the *only* reason any HTTP server exists in this project:

- the **Socket Mode handler** — the sole Slack event path (unchanged), and
- a **minimal `aiohttp` health server** exposing `GET /health` → `200 {"status": "ok"}`
  on `0.0.0.0:$PORT` (`PORT` is set by Render and read from the environment, never
  hardcoded). It carries no Slack traffic and needs no signature verification — it
  exists purely for Render's service-level health check and the keep-alive ping below.

Both run concurrently in the same process (`asyncio.gather` in `src/reach_bot/app.py`),
alongside the periodic message-retention cleanup task. Render restarts the service
automatically on crashes.

### `render.yaml`

```yaml
services:
  - type: web
    name: reach-em
    runtime: python
    plan: free
    buildCommand: "uv sync"
    startCommand: "uv run python app.py"
    healthCheckPath: /health
    envVars:
      - key: SLACK_BOT_TOKEN
      - key: SLACK_APP_TOKEN
      - key: DATABASE_URL
```

The project is managed with `uv` (`pyproject.toml` + `uv.lock`), so the build uses
`uv sync` (no dev extras in the deployed image). The start command launches the single
process that runs **both** the Socket Mode handler and the health server.

### Environment variables

Set these as **secrets in the Render dashboard** — never in the repo or in
`render.yaml`:

| Variable          | Purpose                                                                                                         |
|-------------------|-----------------------------------------------------------------------------------------------------------------|
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-…`)                                                                                            |
| `SLACK_APP_TOKEN` | App-level token (`xapp-…`) for the Socket Mode connection — still required here, unlike an HTTP-mode deployment |
| `DATABASE_URL`    | Supabase/Postgres connection string                                                                             |

`PORT` is set automatically by Render. `REDIS_URL` remains optional (used only by the
dormant presence cache). `SLACK_SIGNING_SECRET` is **not** needed — it applies only to
HTTP-mode request verification.

### Keep-alive

Render's free tier spins idle web services down after ~15 minutes of inactivity, and
Render does not officially support synthetic traffic as a permanent uptime mechanism.
The repository includes `.github/workflows/keep-alive.yml` as a best-effort backup that
pings `${RENDER_APP_URL}/health` with `curl -f` every 10 minutes during its configured
UTC window. GitHub scheduled workflows can be delayed or dropped, so this is not a
hard real-time scheduler.

1. Set `RENDER_APP_URL` as a **repo variable** in the GitHub repository's
   *Settings → Variables* (a plain, non-sensitive URL — hence a variable, not a
   secret).
2. The cron is `*/10 2-21 * * *` — every 10 minutes, but only 02:00–21:59 UTC. GitHub
   Actions cron always runs in UTC, and the 10-minute cadence is deliberately tighter
   than Render's ~15-minute idle window to leave margin for Actions' scheduling
   jitter.
3. **Why the quiet window:** the free plan caps usage at 750 total instance-hours per
   month; pinging 24/7 would burn nearly the entire budget on keep-alive traffic alone.
   The excluded hours (22:00–01:59 UTC) map to 00:00–04:00 in the app owner's local
   timezone (UTC+2) — local 00:00–04:00 at UTC+2 is 22:00–02:00 UTC. If that timezone
   or the quiet hours ever change, redo the UTC+2 → UTC conversion before touching the
   cron hours.
4. **Why uptime matters here:** besides slow first responses after a spin-down, a
   spin-down silently kills any in-flight background broadcast fan-out (it is an
   in-process `asyncio` task), so recipients mid-fan-out would never be pinged with no
   visible error.
5. **Accepted tradeoff (deliberate, not a bug):** during 00:00–04:00 local time the app
   *will* be allowed to spin down, so a real `/reach` in that window pays a cold start
   (tens of seconds) on its first request instead of an instant response. This is a
   deliberate cost/uptime tradeoff for the free tier — revisit it if it matters in
   practice (narrow the quiet window, or move off the free tier).

### External monitor alternative

For more predictable keep-alive traffic, configure an external HTTP monitor such as
UptimeRobot or cron-job.org to send `GET https://<your-app>.onrender.com/health` and
treat HTTP 200 as success.

For cron-job.org, use `*/10 4-23 * * *` with the `Africa/Kigali` timezone. This keeps
the local 00:00–04:00 quiet window while sending requests every 10 minutes from
04:00 through 23:50 local time. If the scheduler is configured for UTC instead, use
`*/10 2-21 * * *`.

This is operational infrastructure outside the repository; the GitHub workflow can
remain enabled as a second check. A 24/7 external monitor keeps the service awake more
consistently but consumes nearly all of Render's 750 free instance-hours/month, so the
quiet window is intentional.

### Reconnection

No custom reconnection logic is needed: Bolt's `AsyncSocketModeHandler` reconnects
automatically when the process restarts or the container wakes from a Render idle
cycle — look for a fresh "session established" log line after each cold start.

### Database migrations

PostgreSQL migrations in `src/reach_bot/migrations/` run automatically when
`PostgresRepository` starts. The runner records applied versions in
`reach_schema_migrations`, uses a PostgreSQL advisory lock during deploy overlap, and
detects already-satisfied schema changes so it does not repeatedly take DDL locks.
The current migration sequence is:

1. `001_initial.sql` — base tables and indexes
2. `002_response_limit.sql` — response counters and message coordinates
3. `003_broadcast_local_thresholds.sql` — per-broadcast counters and response ledger
4. `004_message_retention.sql` — expiry and deletion tracking
5. `005_repair_broadcast_threshold_state.sql` — repairs persisted broadcast counters

If `DATABASE_URL` is omitted, the app intentionally falls back to in-memory storage;
tracked requests, pings, counters, and retention state are then lost on restart.

### Positioning

The health server plus keep-alive setup is a **free-tier workaround, not a permanent
architectural stance.** On a paid tier a proper Background Worker (or the
earlier-considered Oracle Cloud VM path) would make the health server and the
keep-alive ping unnecessary, and the app would run as a plain Socket Mode process.
