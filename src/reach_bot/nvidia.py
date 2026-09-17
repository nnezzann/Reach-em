from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class NvidiaClientError(Exception):
    """An expected, user-actionable NVIDIA API failure."""


class NvidiaClient:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://integrate.api.nvidia.com/v1",
        model: str = "nvidia/nemotron-3.5-lightning-30b-a3b",
        timeout_seconds: float = 30.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._session = session

    async def complete(self, messages: list[dict[str, str]]) -> str:
        if not self.api_key:
            raise NvidiaClientError(
                "The assistant is not configured yet: NVIDIA_API_KEY is missing."
            )
        payload = {"model": self.model, "messages": messages}
        url = f"{self.base_url}/chat/completions"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        owns_session = self._session is None
        session = self._session or aiohttp.ClientSession(timeout=timeout)
        started = time.perf_counter()
        try:
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            ) as response:
                if response.status < 200 or response.status >= 300:
                    logger.error(
                        "NVIDIA API response status=%s latency_ms=%d",
                        response.status,
                        int((time.perf_counter() - started) * 1000),
                    )
                    raise NvidiaClientError(
                        "The assistant service returned an error. Please try again later."
                    )
                try:
                    data: Any = await response.json()
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    logger.error("NVIDIA API returned malformed JSON")
                    raise NvidiaClientError(
                        "The assistant service returned an invalid response."
                    ) from exc
        except TimeoutError as exc:
            logger.error(
                "NVIDIA API timeout latency_ms=%d",
                int((time.perf_counter() - started) * 1000),
            )
            raise NvidiaClientError(
                "The assistant took too long to respond. Please try again."
            ) from exc
        except aiohttp.ClientError as exc:
            logger.error(
                "NVIDIA API transport error category=%s latency_ms=%d",
                type(exc).__name__,
                int((time.perf_counter() - started) * 1000),
            )
            raise NvidiaClientError(
                "The assistant service is unavailable. Please try again."
            ) from exc
        finally:
            if owns_session:
                await session.close()

        try:
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise KeyError("content")
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("NVIDIA API response did not contain assistant content")
            raise NvidiaClientError("The assistant returned an invalid response.") from exc
        logger.info(
            "NVIDIA API success latency_ms=%d",
            int((time.perf_counter() - started) * 1000),
        )
        return content.strip()
