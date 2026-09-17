from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk import WebClient

from reach_bot.config import get_settings
from reach_bot.handlers import register_handlers
from reach_bot.persistence import (
    MemoryRepository,
    PostgresRepository,
    ReachRepository,
    RedisPresenceCache,
)
from reach_bot.ranking import (
    Affinity,
    CachedSignalProvider,
    RankedCandidates,
    SignalProvider,
    rank_candidates,
)
from reach_bot.rendering import render_suggestions
from reach_bot.slack_provider import SlackSignalProvider

settings = get_settings()
logging.basicConfig(level=settings.log_level)


class EmptySignalProvider:
    def public_channels(self, target_id: str) -> list[str]:
        return []

    def channel_members(self, channel_id: str) -> list[str]:
        return []

    def thread_cooccurrences(
        self, target_id: str, channel_ids: list[str], since: Any
    ) -> dict[str, float]:
        return {}

    def presence(self, user_id: str) -> str:
        return "offline"

    def affinities(self, target_id: str) -> dict[str, Affinity]:
        return {}


repository: ReachRepository = MemoryRepository()
if settings.database_url:
    import psycopg

    repository = PostgresRepository(psycopg.connect(settings.database_url))
provider: SignalProvider = SlackSignalProvider(
    WebClient(token=settings.slack_bot_token),
    affinity_source=repository.affinities,
)
if settings.redis_url:
    import redis

    provider = CachedSignalProvider(
        provider,
        RedisPresenceCache(redis.Redis.from_url(settings.redis_url)),
        settings.presence_cache_ttl_seconds,
    )
slack_app = AsyncApp(token=settings.slack_bot_token, signing_secret=settings.slack_signing_secret)
slack_handler = AsyncSlackRequestHandler(slack_app)
api = FastAPI(title="Reach'em", version="0.1.0")


def do_rank(target_id: str, requester_id: str) -> RankedCandidates:
    return rank_candidates(
        target_id,
        requester_id,
        provider,
        max_per_bucket=settings.max_per_bucket,
        min_sample_threshold=settings.min_sample_threshold,
        thread_recency_days=settings.thread_recency_days,
        include_threads=settings.include_thread_signal,
    )


register_handlers(slack_app, repository=repository, ranker=do_rank, renderer=render_suggestions)


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@api.api_route("/slack/events", methods=["GET", "POST"])
async def slack_events(request: Request) -> Any:
    return await slack_handler.handle(request)
