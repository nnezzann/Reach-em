from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from slack_sdk import WebClient

from reach_bot.ranking import Affinity


class SlackSignalProvider:
    """Public-channel-only Slack API adapter used by the pure ranking engine."""

    def __init__(
        self, client: WebClient, affinity_source: Any | None = None
    ) -> None:
        self.client = client
        self.affinity_source = affinity_source

    def public_channels(self, target_id: str) -> list[str]:
        result = self.client.users_conversations(
            user=target_id, types="public_channel", exclude_archived=True
        )
        channels: list[dict[str, Any]] = result.get("channels", [])
        return [str(channel["id"]) for channel in channels if not channel.get("is_private", False)]

    def resolve_user(self, target: str) -> str:
        normalized = target.strip().strip("<@>").removeprefix("@").lower()
        if normalized.startswith(("u", "w")) and normalized[1:].isalnum():
            return normalized.upper()
        response = self.client.users_list(limit=1000)
        members: list[dict[str, Any]] = response.get("members", [])
        for user in members:
            names = {
                str(user.get("name", "")).lower(),
                str(user.get("real_name", "")).lower(),
                str(user.get("profile", {}).get("display_name", "")).lower(),
            }
            if normalized in names and user.get("id"):
                return str(user["id"])
        raise ValueError(f"Slack user @{normalized} was not found.")

    def channel_members(self, channel_id: str) -> list[str]:
        result = self.client.conversations_members(channel=channel_id)
        members: list[str] = result.get("members", [])
        return [str(user_id) for user_id in members]

    def thread_cooccurrences(
        self, target_id: str, channel_ids: list[str], since: datetime
    ) -> dict[str, float]:
        cutoff = since.timestamp()
        scores: dict[str, float] = {}
        for channel_id in channel_ids:
            history = self.client.conversations_history(
                channel=channel_id, oldest=str(cutoff), limit=200
            )
            messages: list[dict[str, Any]] = history.get("messages", [])
            for message in messages:
                if str(message.get("ts", "0")) < str(cutoff):
                    continue
                if not message.get("thread_ts"):
                    continue
                replies = self.client.conversations_replies(
                    channel=channel_id, ts=message["thread_ts"], limit=200
                )
                replies_list: list[dict[str, Any]] = replies.get("messages", [])
                if not any(reply.get("user") == target_id for reply in replies_list):
                    continue
                for reply in replies_list:
                    user_id = reply.get("user")
                    if user_id and user_id != target_id:
                        age_days = max(
                            (
                                datetime.now(since.tzinfo)
                                - datetime.fromtimestamp(float(reply["ts"]), since.tzinfo)
                            ).total_seconds()
                            / 86400,
                            0,
                        )
                        scores[user_id] = max(scores.get(user_id, 0.0), 1.0 / (1.0 + age_days))
        return scores

    def presence(self, user_id: str) -> str:
        return (
            "active"
            if self.client.users_getPresence(user=user_id).get("presence") == "active"
            else "offline"
        )

    def affinities(self, target_id: str) -> dict[str, Affinity]:
        if self.affinity_source is None:
            return {}
        return cast(dict[str, Affinity], self.affinity_source(target_id))
