# Reach'em

Reach'em is a Slack bot that helps a requester reach the smallest useful audience when a teammate is unavailable. It suggests a short list of candidates and only sends a direct message after the requester explicitly chooses and confirms a recipient.

## Development

The project targets Python 3.12+ and exposes:

- `GET /health` for deployment health checks
- `POST /slack/events` for Slack slash commands and interactions

Create a virtual environment and install development dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
cp .env.example .env
```

Set `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` in `.env`, then run:

```bash
uvicorn reach_bot.app:api --reload
```

Run checks with:

```bash
ruff check .
mypy src
pytest
```

The Slack app is intentionally only scaffolded at this stage. Candidate discovery and the interactive ephemeral Block Kit flow will be implemented as separate atomic changes.
