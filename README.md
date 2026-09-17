# Reach'em

Reach'em is a conversational Slack assistant available in a direct message. Send it a normal DM or use `/reach <request>` in that DM; responses stay in the DM thread. The older public-channel candidate ranking components remain available for compatibility, but `/reach` outside a DM only explains how to use the assistant and never ranks or posts in a channel.

## Development

Python 3.12+, `uv`, PostgreSQL 15+, and Redis are supported. Copy `.env.example` to `.env` and set Slack credentials (plus `DATABASE_URL`/`REDIS_URL` in deployed environments). No tokens or message contents are logged.

```bash
uv sync --extra dev
uv run uvicorn reach_bot.app:api --reload
uv run ruff check .
uv run mypy src
uv run pytest
```

The NVIDIA-compatible API uses `NVIDIA_API_KEY`, with configurable `NVIDIA_BASE_URL` and `NVIDIA_MODEL`. Logs are timestamped and written to stderr by default; set `LOG_FILE` to also write a local log file (parent directories are created as needed). Logs contain operational metadata only, never API keys or message contents.

For real-time local Slack CLI testing, use a slash-safe copy path because the repository name contains an apostrophe:

```bash
rm -rf /tmp/reach-em-live
cp -a "$PWD" /tmp/reach-em-live
cd /tmp/reach-em-live
uv run slack run
```

The ranking engine keeps presence, inverse public-channel size, and optional recent thread co-occurrence as separate evidence. Affinity is only a capped boost after the configured sample threshold; each presence bucket is capped by `MAX_PER_BUCKET`. PostgreSQL migrations are in `src/reach_bot/migrations/`. Redis stores only short-TTL `presence:{user_id}` values.

All suggestions are ephemeral. The bot never posts to public channels, reads private channels or DMs, or displays affinity as a leaderboard.
