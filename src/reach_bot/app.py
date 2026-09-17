import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp

from reach_bot.config import get_settings

settings = get_settings()
logging.basicConfig(level=settings.log_level)

slack_app = AsyncApp(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
)
slack_handler = AsyncSlackRequestHandler(slack_app)
api = FastAPI(title="Reach'em", version="0.1.0")


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@api.api_route("/slack/events", methods=["GET", "POST"])
async def slack_events(request: Request) -> Any:
    return await slack_handler.handle(request)


@slack_app.command("/reach")
async def reach_command(ack: Callable[[str], Awaitable[None]]) -> None:
    await ack("Reach is not configured yet. Candidate discovery is coming next.")
