"""Tests for the Render keep-alive health endpoint.

The health server exists only so Render's free-tier Web Service stays up
(Render health check + external keep-alive ping); it carries no Slack
traffic. See the Deployment section of AGENTS.md / README.md.
"""

from aiohttp.test_utils import TestClient, TestServer

from reach_bot.app import create_health_app


async def test_health() -> None:
    """GET /health answers 200 with the trivial ok body."""
    async with TestClient(TestServer(create_health_app())) as client:
        response = await client.get("/health")
        body = await response.json()

    assert response.status == 200
    assert body == {"status": "ok"}
