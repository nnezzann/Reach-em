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

    def _iter_members(self) -> list[dict[str, Any]]:
        """Fetch the full workspace member list, following cursor pagination.

        The previous implementation used a single `users_list(limit=1000)` call,
        which silently drops members past the first page on larger workspaces.
        """
        members: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            response = self.client.users_list(limit=200, cursor=cursor or "")
            members.extend(response.get("members", []))
            cursor = response.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                break
        return members

    def resolve_user(self, target: str) -> str:
        """Resolve a slash-command target into a Slack user ID.

        The happy path is a real `<@Uxxxx|name>` mention, which Slack sends
        automatically when the user is picked from its own @mention
        autocomplete -- that's unambiguous and needs no lookup at all.

        The fallback below only fires when someone typed a name as plain text
        instead of picking from the dropdown. It matches *exactly* against
        username / real name / display name and nothing looser: guessing at
        the wrong person is worse than failing loudly, since this app pings a
        real human on someone's behalf. On no match (or an ambiguous one), it
        raises with guidance to use the autocomplete instead of trying to be
        clever about it.
        """
        normalized = target.strip().strip("<@>").removeprefix("@").lower()

        # Already a raw user/workspace ID (from a resolved <@U123|name> mention,
        # or someone pasting an ID directly).
        if normalized.startswith(("u", "w")) and normalized[1:].isalnum():
            return normalized.upper()

        members = self._iter_members()
        matches = [
            user
            for user in members
            if user.get("id")
            and normalized
            in {
                str(user.get("name", "")).lower(),
                str(user.get("real_name", "")).lower(),
                str(user.get("profile", {}).get("display_name", "")).lower(),
            }
        ]

        if len(matches) == 1:
            return str(matches[0]["id"])
        if len(matches) > 1:
            # Exact-match collisions are rare but possible (e.g. duplicate
            # display names) -- surface it rather than silently picking one.
            options = ", ".join(sorted({u.get("name", "") for u in matches}))
            raise ValueError(
                f"Multiple Slack users match '{normalized}' ({options}). "
                "Pick the person from Slack's @mention autocomplete instead."
            )

        raise ValueError(
            f"Slack user @{normalized} was not found. "
            "Start typing @ and pick them from Slack's autocomplete list "
            "so the name resolves correctly."
        )

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
