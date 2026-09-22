"""Tests for the Render keep-alive health endpoint.

The health server exists only so Render's free-tier Web Service stays up
(Render health check + external keep-alive ping); it carries no Slack
traffic. See the Deployment section of AGENTS.md / README.md.
"""

import logging

from aiohttp.test_utils import TestClient, TestServer

from reach_bot.app import create_health_app


async def test_health(caplog) -> None:
    """GET /health answers 200 and logs the request."""
    caplog.set_level(logging.INFO, logger="reach_bot.app")

    async with TestClient(TestServer(create_health_app())) as client:
        response = await client.get("/health")
        body = await response.json()

    assert response.status == 200
    assert body == {"status": "ok"}
    assert "GET /health" in caplog.text
