from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from reach_bot import broadcast


class FakeClient:
    def __init__(
        self,
        channel_members: dict[str, list[str]] | None = None,
        users: list[dict[str, Any]] | None = None,
    ) -> None:
        self.channel_members = channel_members or {}
        self.users = users or []
        self.sent: list[dict[str, Any]] = []

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        members = self.channel_members[str(kwargs["channel"])]
        if not kwargs.get("cursor"):
            return {"members": members[:2], "response_metadata": {"next_cursor": "next"}}
        return {"members": members[2:], "response_metadata": {}}

    async def users_list(self, **kwargs: Any) -> dict[str, Any]:
        if not kwargs.get("cursor"):
            return {"users": self.users[:2], "response_metadata": {"next_cursor": "next"}}
        return {"users": self.users[2:], "response_metadata": {}}

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


def test_channel_members_follows_pagination(monkeypatch) -> None:
    monkeypatch.setattr(broadcast, "SEND_DELAY_SECONDS", 0.0)
    client = FakeClient(channel_members={"C1": ["U1", "U2", "U3"]})

    assert asyncio.run(broadcast.channel_members(client, "C1")) == ["U1", "U2", "U3"]


def test_workspace_members_skip_bots_deleted_and_app_users(monkeypatch) -> None:
    monkeypatch.setattr(broadcast, "SEND_DELAY_SECONDS", 0.0)
    client = FakeClient(
        users=[
            {"id": "U1"},
            {"id": "U2", "is_bot": True},
            {"id": "U3", "deleted": True},
            {"id": "U4", "is_app_user": True},
            {"id": "U5"},
        ]
    )

    assert asyncio.run(broadcast.workspace_members(client)) == ["U1", "U5"]


def test_fan_out_sends_to_channel_except_excluded(monkeypatch) -> None:
    monkeypatch.setattr(broadcast, "SEND_DELAY_SECONDS", 0.0)
    client = FakeClient(channel_members={"C1": ["U-target", "U-requester", "U-a", "U-b"]})
    repo = FakeRepo()

    sent = asyncio.run(
        broadcast.fan_out_broadcast(
            client,
            repo,
            reach_request_id="req-1",
            requester_id="U-requester",
            target_id="U-target",
            scope="channel",
            channel_id="C1",
            message="Anyone seen them?",
            exclude={"U-target", "U-requester"},
            known_response_limit=3,
        )
    )

    assert sent == 2
    assert repo.pings == ["U-a", "U-b"]
    assert repo.contexts == ["C1", "C1"]
    assert [message["channel"] for message in client.sent] == ["U-a", "U-b"]
    text = client.sent[0]["text"]
    assert "Reach, relaying for <@U-requester> about <@U-target>" in text
    actions = client.sent[0]["blocks"][1]["elements"]
    assert [element["action_id"] for element in actions] == [
        "outcome_helped",
        "outcome_unknown",
        "outcome_more",
    ]
    assert repo.deliveries == ["ping-1", "ping-2"]


def test_fan_out_stops_once_response_limit_reached(monkeypatch) -> None:
    monkeypatch.setattr(broadcast, "SEND_DELAY_SECONDS", 0.0)
    client = FakeClient(channel_members={"C1": ["U-a", "U-b", "U-c"]})
    repo = FakeRepo(known=3)

    sent = asyncio.run(
        broadcast.fan_out_broadcast(
            client,
            repo,
            reach_request_id="req-1",
            requester_id="U-requester",
            target_id="U-target",
            scope="channel",
            channel_id="C1",
            message="hi",
            exclude=set(),
            known_response_limit=3,
        )
    )

    assert sent == 0
    assert repo.pings == []
    assert client.sent == []


def test_fan_out_workspace_scope_uses_user_listing(monkeypatch) -> None:
    monkeypatch.setattr(broadcast, "SEND_DELAY_SECONDS", 0.0)
    client = FakeClient(users=[{"id": "U-a"}, {"id": "U-b", "is_bot": True}])
    repo = FakeRepo()

    sent = asyncio.run(
        broadcast.fan_out_broadcast(
            client,
            repo,
            reach_request_id="req-1",
            requester_id="U-requester",
            target_id="U-target",
            scope="workspace",
            channel_id="",
            message="hi",
            exclude={"U-requester", "U-target"},
            known_response_limit=3,
        )
    )

    assert sent == 1
    assert repo.contexts == ["workspace"]
    assert repo.pings == ["U-a"]
