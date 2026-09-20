"""Background fan-out for the channel/workspace broadcast scopes.

Recipients reached through a broadcast get exactly the same DM, ping
record, and three response actions as hand-picked ones — the response
cutoff counts them together, per reach request.
"""

from __future__ import annotations

import asyncio
import logging

from slack_sdk.errors import SlackApiError

from reach_bot.persistence import ReachRepository
from reach_bot.rendering import mrkdwn_section, render_recipient_actions

log = logging.getLogger(__name__)

# Polite pacing for bulk DMs; Slack rate-limits chat.postMessage heavily.
SEND_DELAY_SECONDS = 1.0


async def channel_members(client: object, channel_id: str) -> list[str]:
    """All member IDs of a channel, following cursor pagination."""
    member_ids: list[str] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, object] = {"channel": channel_id, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        response = await client.conversations_members(**kwargs)  # type: ignore[attr-defined]
        member_ids.extend(str(member) for member in response.get("members", []))
        cursor = response.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            return member_ids


async def workspace_members(client: object) -> list[str]:
    """Active human workspace member IDs, following cursor pagination."""
    member_ids: list[str] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, object] = {"limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        response = await client.users_list(**kwargs)  # type: ignore[attr-defined]
        for member in response.get("users", []):
            if member.get("deleted") or member.get("is_bot") or member.get("is_app_user"):
                continue
            member_ids.append(str(member["id"]))
        cursor = response.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            return member_ids


async def fan_out_broadcast(
    client: object,
    repository: ReachRepository,
    *,
    reach_request_id: str,
    requester_id: str,
    target_id: str,
    scope: str,
    channel_id: str,
    message: str,
    exclude: set[str],
    known_response_limit: int,
) -> int:
    """DM the broadcast audience; returns the number of messages sent.

    Stops early once the per-request "I know" cutoff is reached so a
    confirmed target stops generating new interruptions.
    """
    if scope == "channel":
        context = channel_id
        recipient_ids = await channel_members(client, channel_id)
    elif scope == "workspace":
        context = "workspace"
        recipient_ids = await workspace_members(client)
    else:
        return 0

    # Mentions are built server-side from stored IDs, identical to the
    # hand-picked send path.
    text = f"Reach, relaying for <@{requester_id}> about <@{target_id}>:\n{message}"
    sent = 0
    for user_id in dict.fromkeys(recipient_ids):
        if user_id in exclude:
            continue
        if repository.known_count(reach_request_id) >= known_response_limit:
            log.info(
                "broadcast fan-out stopped early: response limit reached for request %s",
                reach_request_id,
            )
            break
        ping = await asyncio.to_thread(
            repository.create_ping,
            reach_request_id,
            requester_id,
            target_id,
            user_id,
            context,
            "unknown",
        )
        try:
            response = await client.chat_postMessage(  # type: ignore[attr-defined]
                channel=user_id,
                text=text,
                blocks=[
                    mrkdwn_section(text),
                    render_recipient_actions(
                        ping_id=ping.id,
                        reach_request_id=reach_request_id,
                        requester_id=requester_id,
                        target_id=target_id,
                        candidate_id=user_id,
                    ),
                ],
            )
        except SlackApiError as exc:
            log.warning("broadcast DM to %s failed: %s", user_id, exc)
            continue
        await asyncio.to_thread(
            repository.set_ping_delivery,
            ping.id,
            str(response.get("channel", "")),
            str(response.get("ts", "")),
        )
        sent += 1
        await asyncio.sleep(SEND_DELAY_SECONDS)
    return sent
