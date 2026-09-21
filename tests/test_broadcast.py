from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from reach_bot import broadcast


class FakeClient:
    def __init__(
        self,
        channels: list[dict[str, Any]] | None = None,
    ) -> None:
        self.channels = channels or []
        self.sent: list[dict[str, Any]] = []

    async def conversations_list(self, **kwargs: Any) -> dict[str, Any]:
        if not kwargs.get("cursor"):
            return {"channels": self.channels[:2], "response_metadata": {"next_cursor": "next"}}
        return {"channels": self.channels[2:], "response_metadata": {}}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        return {"channel": str(kwargs["channel"]), "ts": f"{len(self.sent)}.0001"}


class FakeRepo:
    def __init__(self, known: int = 0) -> None:
        self.known = known
        self.pings: list[str] = []
        self.contexts: list[Any] = []
        self.deliveries: list[str] = []

    def create_ping(
        self,
        reach_request_id: str,
        requester_id: str,
        target_id: str,
        candidate_id: str,
        channel_context: Any,
        presence: str,
    ) -> Any:
        self.pings.append(candidate_id)
        self.contexts.append(channel_context)
        return SimpleNamespace(id=f"ping-{len(self.pings)}")

    def set_ping_delivery(self, ping_id: str, channel: str, message_ts: str) -> None:
        self.deliveries.append(ping_id)

    def known_count(self, reach_request_id: str) -> int:
        return self.known


def test_workspace_public_channels_follows_pagination() -> None:
    client = FakeClient(
        channels=[
            {"id": "C1", "is_channel": True, "is_archived": False},
            {"id": "C2", "is_channel": True, "is_archived": True},
            {"id": "C3", "is_channel": True, "is_archived": False},
        ]
    )

    assert asyncio.run(broadcast.workspace_public_channels(client)) == ["C1", "C3"]


def test_post_to_channels_posts_to_selected_channels() -> None:
    client = FakeClient()
    repo = FakeRepo()

    posted = asyncio.run(
        broadcast.post_to_channels(
            client,
            repo,
            reach_request_id="req-1",
            requester_id="U-requester",
            target_id="U-target",
            scope="channel",
            channel_ids=["C1", "C2"],
            message="Anyone seen them?",
            known_response_limit=3,
        )
    )

    assert posted == 2
    assert sorted(message["channel"] for message in client.sent) == ["C1", "C2"]
    text = client.sent[0]["text"]
    assert "Reach, relaying for <@U-requester> about <@U-target>" in text
    assert text.startswith("<!channel>")
    actions = client.sent[0]["blocks"][1]["elements"]
    assert [element["action_id"] for element in actions] == [
        "outcome_helped",
        "outcome_unknown",
        "outcome_more",
    ]
    assert repo.pings == ["", ""]
    assert repo.contexts == ["broadcast", "broadcast"]
    assert sorted(repo.deliveries) == ["ping-1", "ping-2"]


def test_post_to_channels_stops_once_response_limit_reached() -> None:
    client = FakeClient()
    repo = FakeRepo(known=3)

    posted = asyncio.run(
        broadcast.post_to_channels(
            client,
            repo,
            reach_request_id="req-1",
            requester_id="U-requester",
            target_id="U-target",
            scope="channel",
            channel_ids=["C1", "C2"],
            message="hi",
            known_response_limit=3,
        )
    )

    assert posted == 0
    assert repo.pings == []
    assert client.sent == []


def test_post_to_channels_workspace_scope_resolves_public_channels() -> None:
    client = FakeClient(
        channels=[
            {"id": "C1", "is_channel": True, "is_archived": False},
            {"id": "C2", "is_channel": True, "is_archived": False},
        ]
    )
    repo = FakeRepo()

    posted = asyncio.run(
        broadcast.post_to_channels(
            client,
            repo,
            reach_request_id="req-1",
            requester_id="U-requester",
            target_id="U-target",
            scope="workspace",
            channel_ids=[],
            message="hi",
            known_response_limit=3,
        )
    )

    assert posted == 2
    assert sorted(message["channel"] for message in client.sent) == ["C1", "C2"]
