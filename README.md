# Reach'em

Reach'em is a Slack bot that helps a requester reach the smallest useful audience when a teammate is unavailable. It suggests a short, ranked list from **public-channel** signals and only sends a direct message after the requester explicitly chooses and confirms a recipient.

## Development

Python 3.12+, `uv`, PostgreSQL 15+, and Redis are supported. Copy `.env.example` to `.env` and set Slack credentials (plus `DATABASE_URL`/`REDIS_URL` in deployed environments). No tokens or message contents are logged.

```bash
uv sync --extra dev
cp .env.example .env
```

For local development, enable Socket Mode in the Slack app and set both tokens in
`.env`: `SLACK_BOT_TOKEN` is the bot token (`xoxb-...`) and `SLACK_APP_TOKEN` is
the app-level Socket Mode token (`xapp-...`). The checked-in `manifest.json` has
`socket_mode_enabled` set to `true`; apply it to the Slack app before running.
Then start the bot with:

```bash
uv run python app.py
```

The process stays connected through `AsyncSocketModeHandler`; no public URL or
`SLACK_SIGNING_SECRET` is needed for Socket Mode. Keep `.env` local and never
commit real tokens.

The FastAPI `/slack/events` endpoint remains available for deployments that
explicitly omit `SLACK_APP_TOKEN` and run `uv run uvicorn reach_bot.app:api
--host 0.0.0.0 --port 8000`. In that HTTP mode, configure Slack request URLs
for `/slack/events` and set `SLACK_SIGNING_SECRET`.

The ranking engine keeps presence, inverse public-channel size, and optional recent thread co-occurrence as separate evidence. Affinity is only a capped boost after the configured sample threshold; each presence bucket is capped by `MAX_PER_BUCKET`. PostgreSQL migrations are in `src/reach_bot/migrations/`. Redis stores only short-TTL `presence:{user_id}` values.

Run checks with:

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

All suggestions are ephemeral. The bot never posts to public channels, reads private channels or DMs, or displays affinity as a leaderboard.
