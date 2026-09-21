"""Broadcast scope delivery for Reach.

Replaced the old per-recipient DM fan-out with public-channel posts:
- "channel" scope: posts to one or more selected public channels.
- "workspace" scope: resolves all public workspace channels and posts
  one message per channel.
- Hand-picked recipients remain unchanged (individual DMs via the
  existing fast synchronous path in handlers.py).

Each channel post uses `<!channel>` mention syntax (not `<@channel>`,
which is a literal user mention and will not notify anyone), carries the
same three-button `actions` block, and is tracked as a broadcast ping so
it participates in the threshold sweep and cleanup rules.
"""

from __future__ import annotations

import asyncio
import logging

from slack_sdk.errors import SlackApiError

from reach_bot.persistence import ReachRepository
from reach_bot.rendering import broadcast_text, mrkdwn_section, render_recipient_actions

log = logging.getLogger(__name__)

# Bounded concurrency for broadcast posts to public channels.
# Keeps the fan-out well under Slack's per-workspace rate limits without
# making large workspace broadcasts impossibly slow.
MAX_CONCURRENT_CHANNEL_POSTS = 5


async def workspace_public_channels(client: object) -> list[str]:
    """Resolve all public (non-archived) workspace channels via paginated `conversations.list`."""
    channels: list[str] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, object] = {"types": "public_channel", "exclude_archived": True, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        response = await client.conversations_list(**kwargs)  # type: ignore[attr-defined]
        for ch in response.get("channels", []):
            if ch.get("is_channel") and not ch.get("is_archived"):
                channels.append(str(ch.get("id", "")))
        cursor = response.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            break
    return channels


async def post_to_channels(
    client: object,
    repository: ReachRepository,
    *,
    reach_request_id: str,
    requester_id: str,
    target_id: str,
    scope: str,
    channel_ids: list[str],
    message: str,
    known_response_limit: int,
) -> int:
    """Post the composed message to each selected/resolved public channel.

    Returns the number of channels successfully posted to.
    Tracks each post as a broadcast `Ping` so it participates in the
    three-response cutoff sweep and per-message cleanup.
    """
    if not channel_ids:
        return 0

    # Resolve workspace public channels when scope is "workspace".
    if scope == "workspace":
        resolved = await workspace_public_channels(client)
        # Deduplicate and filter out any empty IDs.
        resolved = [str(ch) for ch in dict.fromkeys(resolved) if ch]
        # If no public channels found, nothing to post.
        if not resolved:
            return 0
        channel_ids = resolved

    text = broadcast_text(message, requester_id, target_id)
    posted = 0
    # Use a bounded semaphore to avoid overwhelming Slack rate limits.
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHANNEL_POSTS)
    tasks: list[asyncio.Task[int]] = []

    async def post_single(channel_id: str) -> int:
        async with semaphore:
            # Check the threshold before posting; if the limit has already
            # been reached, skip further posts.
            if repository.known_count(reach_request_id) >= known_response_limit:
                return 0
            # Create a broadcast ping — no pre-known recipient, identity
            # will be read lazily from the clicker's user at response time.
            ping = await asyncio.to_thread(
                repository.create_ping,
                reach_request_id,
                requester_id,
                target_id,
                "",  # No single candidate — broadcast to a channel
                "broadcast",
                "unknown",
            )
            # Store the broadcast context (channel ID) on the ping so the
            # threshold sweep and per-message cleanup know where to find
            # the message.
            ping_with_context = ping  # The ping id is what matters; context added below via delivery.
            try:
                response = await client.chat_postMessage(
                    channel=channel_id,
                    text=text,
                    blocks=[
                        mrkdwn_section(text),
                        render_recipient_actions(
                            ping_id=ping.id,
                            reach_request_id=reach_request_id,
                            requester_id=requester_id,
                            target_id=target_id,
                            candidate_id=channel_id,  # Channel acts as the "recipient" key for broadcast messages.
                        ),
                    ],
                )
            except SlackApiError as exc:
                log.warning("Broadcast post to %s failed: %s", channel_id, exc)
                return 0
            await asyncio.to_thread(
                repository.set_ping_delivery,
                ping.id,
                str(channel_id),  # channel = the public channel ID (used for chat.update sweep)
                str(response.get("ts", "")),
            )
            return 1

    for ch in channel_ids:
        task = asyncio.create_task(post_single(ch))
        tasks.append(task)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, int) and result > 0:
            posted += result
        elif isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            log.error("Broadcast post exception: %s", result)
    return posted
