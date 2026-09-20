"""Reach application composition root.

The ranking/suggestion machinery (``reach_bot.ranking``,
``reach_bot.affinity``, ``reach_bot.slack_provider``) is dormant: the
/reach flow no longer computes candidates. The modules and their tests
are kept for possible future reuse.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, Request
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from reach_bot.config import get_settings
from reach_bot.handlers import register_handlers
from reach_bot.persistence import MemoryRepository, PostgresRepository, ReachRepository

settings = get_settings()
logging.basicConfig(level=settings.log_level)

repository: ReachRepository = MemoryRepository()
if settings.database_url:
    import psycopg

    repository = PostgresRepository(psycopg.connect(settings.database_url))

slack_app = AsyncApp(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
    request_verification_enabled=settings.slack_signing_secret is not None,
)
slack_handler = AsyncSlackRequestHandler(slack_app)
api = FastAPI(title="Reach'em", version="0.1.0")

register_handlers(
    slack_app,
    repository=repository,
    known_response_limit=settings.known_response_limit,
)


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@api.api_route("/slack/events", methods=["GET", "POST"])
async def slack_events(request: Request) -> Any:
    return await slack_handler.handle(request)


async def run_socket_mode(app_token: str) -> None:
    handler = AsyncSocketModeHandler(slack_app, app_token)
    await handler.start_async()  # type: ignore[no-untyped-call]


if __name__ == "__main__":
    if settings.slack_app_token:
        asyncio.run(run_socket_mode(settings.slack_app_token))
    else:
        import uvicorn

        uvicorn.run(api, host=settings.host, port=settings.port)
