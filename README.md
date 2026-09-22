# Reach'em

Reach'em is a Slack bot that helps a requester reach the smallest useful audience when a teammate is unavailable. Instead of posting in a shared channel and interrupting dozens of people, the bot allows requesters to manually select specific people to reach via direct messages, with optional broadcast scopes for channel or workspace-wide reach.

## Features

- **Manual recipient selection**: Requesters pick specific people to reach via a multi-user picker
- **Broadcast scopes**: Option to reach everyone in a channel or the entire workspace  
- **Per-responder acknowledgment**: DM respondents see an in-place thank-you edit; broadcast respondents get an ephemeral (only-visible-to-them) acknowledgment
- **Three response types**: "I know", "I don't know", and "Custom message" for easy feedback
- **Message retention**: automatically delete each DM or broadcast after a required number of minutes, hours, or days
- **Broadcast cleanup**: once a broadcast reaches its response threshold, the channel post is deleted and the status is shown only ephemerally
- **Two independent closure pools**: manual DM messages close via a hunt-wide 3-"I know" threshold; each broadcast message closes via its own local thresholds (3 "I know" OR 5 total responses, whichever first) — the two counter systems are fully decoupled
- **Privacy-first**: Never posts to public channels, reads private DMs, or displays affinity scores

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

- `commands` - Required for `/reach` slash command
- `chat:write` - Required to send DMs to recipients
- `channels:read` - Required to read public channel members for broadcast
- `groups:read` - Required to read private channel members for broadcast
- `users:read` - Required to resolve user profiles
- `im:write` - Required to send direct messages
- `im:history` - Required for message management

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

5. Optionally configure database and Redis for production features:

```
DATABASE_URL=postgresql://user:password@localhost/reachem
REDIS_URL=redis://localhost:6379
```

### Running the Bot

Run the app (Socket Mode handler + health server, in one process):

```bash
uv run python app.py
```

The bot will connect to Slack via Socket Mode and respond to `/reach` commands. The
health server listens on `0.0.0.0:$PORT` (default 8000) and serves only `GET /health`;
it carries no Slack traffic and exists for Render's health check and the keep-alive ping
(see [Deployment](#deployment)).

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

## Privacy & Security

- No tokens or message contents are logged
- The bot never posts to public channels
- The bot never reads private channels or DMs
- All suggestions are ephemeral (no persistent ranking displayed)
- Affinity scores are kept internal to ranking and never exposed as leaderboards

## Deployment

The bot is deployed as a **Render free-tier Web Service** that talks to Slack entirely
over **Socket Mode** (HTTP mode / Slack Request URLs were considered and explicitly
rejected — there is no webhook endpoint and no `SLACK_SIGNING_SECRET` anywhere in this
deployment).

### Why a Web Service, not a Background Worker

Render's free plan does not offer Background Workers, so the long-lived Socket Mode
process must be deployed as `type: web`. A Web Service has to bind `$PORT` and answer
HTTP requests to stay up, which is the *only* reason any HTTP server exists in this
project:

- the **Socket Mode handler** — the sole Slack event path (unchanged), and
- a **minimal `aiohttp` health server** exposing `GET /health` → `200 {"status": "ok"}`
  on `0.0.0.0:$PORT` (`PORT` is set by Render and read from the environment, never
  hardcoded). It carries no Slack traffic and needs no signature verification — it
  exists purely for Render's service-level health check and the keep-alive ping below.

Both run concurrently in the same process (`asyncio.gather` in `src/reach_bot/app.py`),
and Render restarts the service automatically on crashes.

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

| Variable | Purpose |
| --- | --- |
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-…`) |
| `SLACK_APP_TOKEN` | App-level token (`xapp-…`) for the Socket Mode connection — still required here, unlike an HTTP-mode deployment |
| `DATABASE_URL` | Supabase/Postgres connection string |

`PORT` is set automatically by Render. `REDIS_URL` remains optional (used only by the
dormant presence cache). `SLACK_SIGNING_SECRET` is **not** needed — it applies only to
HTTP-mode request verification.

### Keep-alive (GitHub Actions)

Render's free tier spins idle web services down after ~15 minutes of inactivity, and
Render does not officially support pinging to prevent that. So
`.github/workflows/keep-alive.yml` pings `${RENDER_APP_URL}/health` with `curl -f`
every 10 minutes, failing the run on anything other than 200.

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

### Reconnection

No custom reconnection logic is needed: Bolt's `AsyncSocketModeHandler` reconnects
automatically when the process restarts or the container wakes from a Render idle
cycle — look for a fresh "session established" log line after each cold start.

### Database migrations

Apply `src/reach_bot/migrations/` against the Supabase/Postgres database in order
(`001_initial.sql`, then `002_response_limit.sql`). Migrations are versioned SQL and
there is no in-app migration runner.

### Positioning

The health server plus keep-alive setup is a **free-tier workaround, not a permanent
architectural stance.** On a paid tier a proper Background Worker (or the
earlier-considered Oracle Cloud VM path) would make the health server and the
keep-alive ping unnecessary, and the app would run as a plain Socket Mode process.
