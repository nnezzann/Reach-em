"""Reach application composition root.

The ranking/suggestion machinery (``reach_bot.ranking``,
``reach_bot.affinity``, ``reach_bot.slack_provider``) is dormant: the
/reach flow no longer computes candidates. The modules and their tests
are kept for possible future reuse.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from reach_bot.config import get_settings
from reach_bot.handlers import register_handlers
from reach_bot.persistence import MemoryRepository, PostgresRepository, ReachRepository

settings = get_settings()
logging.basicConfig(level=settings.log_level)

log = logging.getLogger(__name__)

repository: ReachRepository = MemoryRepository()
if settings.database_url:
    import psycopg

    repository = PostgresRepository(psycopg.connect(settings.database_url))

slack_app = AsyncApp(token=settings.slack_bot_token)

register_handlers(
    slack_app,
    repository=repository,
    known_response_limit=settings.known_response_limit,
)


def create_health_app() -> web.Application:
    """Build the minimal health-check web app.

    WHY THIS SERVER EXISTS: Render's Web Service tier only keeps a free-plan
    service alive while a process binds $PORT and answers HTTP requests. This
    server exists solely to satisfy that requirement for Render's health check
    and the external keep-alive ping. It carries NO Slack traffic and is
    unrelated to Bolt's actual event handling, which remains entirely over
    Socket Mode (the AsyncSocketModeHandler started alongside it in ``main``).
    It therefore needs no Slack signature verification: it is not part of
    Slack's request flow at all.
    """

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    application = web.Application()
    application.router.add_get("/health", health)
    return application


async def run_health_server() -> None:
    """Serve the health endpoint for the lifetime of the process."""
    runner = web.AppRunner(create_health_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.host, port=settings.port)
    await site.start()
    log.info(
        "health server listening on %s:%s (GET /health only — no Slack traffic)",
        settings.host,
        settings.port,
    )
    # Serve forever: this coroutine is one half of the asyncio.gather in
    # ``main`` and only returns when the event loop is torn down.
    await asyncio.Event().wait()


async def run_socket_mode(app_token: str) -> None:
    """Connect to Slack over Socket Mode and keep the connection alive.

    Bolt reconnects automatically when the underlying process restarts or
    the container wakes from a Render idle cycle (a fresh "session
    established" log line appears on every process start), so no custom
    reconnection/retry logic is layered on top of it.
    """
    handler = AsyncSocketModeHandler(slack_app, app_token)
    await handler.start_async()  # type: ignore[no-untyped-call]


async def main() -> None:
    """Run the Socket Mode handler and the health server concurrently.

    Both live in this single process (one Render service): Socket Mode is
    the only Slack event path, and the health server exists purely so the
    free-tier Web Service stays up (see the Deployment section of
    AGENTS.md / README.md).
    """
    await asyncio.gather(
        run_socket_mode(settings.slack_app_token),
        run_health_server(),
    )


if __name__ == "__main__":
    asyncio.run(main())
