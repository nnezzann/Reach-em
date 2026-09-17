# Reach'em

Reach'em is a Slack bot that helps a requester reach the smallest useful audience when a teammate is unavailable. It suggests a short, ranked list from **public-channel** signals and only sends a direct message after the requester explicitly chooses and confirms a recipient.

## Development

Python 3.12+, `uv`, PostgreSQL 15+, and Redis are supported. Copy `.env.example` to `.env` and set Slack credentials (plus `DATABASE_URL`/`REDIS_URL` in deployed environments). No tokens or message contents are logged.

```bash
uv sync --extra dev
uv run uvicorn reach_bot.app:api --reload
uv run ruff check .
uv run mypy src
uv run pytest
```

The ranking engine keeps presence, inverse public-channel size, and optional recent thread co-occurrence as separate evidence. Affinity is only a capped boost after the configured sample threshold; each presence bucket is capped by `MAX_PER_BUCKET`. PostgreSQL migrations are in `src/reach_bot/migrations/`. Redis stores only short-TTL `presence:{user_id}` values.

All suggestions are ephemeral. The bot never posts to public channels, reads private channels or DMs, or displays affinity as a leaderboard.
