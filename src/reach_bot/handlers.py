from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from slack_sdk.errors import SlackApiError

from reach_bot.broadcast import post_to_channels
from reach_bot.persistence import Ping, PingOutcome, ReachRepository
from reach_bot.rendering import (
    CUTOFF_STATUS,
    THANK_YOU_STATUS,
    mrkdwn_section,
    render_location_modal,
    render_ping_modal,
    render_quick_channels_modal,
    render_quick_people_modal,
    render_reach_audience,
    render_reach_message,
    render_reach_stage1,
    render_recipient_actions,
    render_reply_modal,
)

log = logging.getLogger(__name__)

# Two independent closure pools (docs §6.2 / §6.4):
#   Pool 1 — manual/hand-picked DM recipients: hunt-wide global counter
#   (`known_response_limit`), unchanged.
#   Pool 2 — broadcast channel/workspace posts: per-message local counters
#   with their own thresholds, fully decoupled from the DM global counter
#   (no shared counter, no cross-influence in either direction).
BROADCAST_KNOW_LIMIT = 3
BROADCAST_TOTAL_LIMIT = 5

# Ephemeral note shown to anyone clicking a broadcast message that has
# already been closed by its local thresholds: no recording, no re-update.
BROADCAST_CLOSED_NOTE = CUTOFF_STATUS
# The explicit cleanup command only targets tracked messages created before
# retention was introduced. It never sweeps arbitrary user messages.
RETENTION_FEATURE_INTRODUCED_AT = datetime(2026, 9, 21, tzinfo=UTC)

# Background fan-out tasks; kept referenced so they are not garbage-collected.
_background_tasks: set[asyncio.Task[Any]] = set()
_USER_TOKEN = re.compile(r"^<@([A-Z0-9]+)>$|^@([A-Za-z0-9_.-]+)$")
_CHANNEL_TOKEN = re.compile(r"^<#([A-Z0-9]+)(?:\|[^>]+)?>$|^#([A-Za-z0-9_.-]+)$")


def _retention_expiry(values: dict[str, Any]) -> tuple[datetime | None, str | None]:
    raw_amount = str(
        values.get("retention_amount", {})
        .get("retention_amount_input", {})
        .get("value", "")
        or ""
    ).strip()
    if not raw_amount:
        return None, "Enter how long the message should remain."
    if not raw_amount.isdigit() or int(raw_amount) <= 0:
        return None, "Enter a positive whole number for the retention time."
    unit = str(
        values.get("retention_unit", {})
        .get("retention_unit_choice", {})
        .get("selected_option", {})
        .get("value", "hours")
    )
    factors = {"minutes": 60, "hours": 3600, "days": 86400}
    if unit not in factors:
        return None, "Choose minutes, hours, or days for the retention unit."
    return datetime.now(UTC) + timedelta(seconds=int(raw_amount) * factors[unit]), None


def _shortcut_tokens(text: str) -> tuple[list[str], list[str]]:
    users: list[str] = []
    channels: list[str] = []
    for raw in text.split():
        token = raw.strip(",")
        if _USER_TOKEN.match(token):
            users.append(token)
        elif _CHANNEL_TOKEN.match(token):
            channels.append(token)
    return users, channels


async def _resolve_user_tokens(client: Any, tokens: list[str]) -> tuple[list[str], str | None]:
    if not tokens:
        return [], None
    canonical_ids = [_USER_TOKEN.match(token) for token in tokens]
    if all(match is not None and match.group(1) for match in canonical_ids):
        return [str(match.group(1)) for match in canonical_ids if match is not None], None
    response = await client.users_list(limit=200)
    users = [
        user
        for user in response.get("members", response.get("users", []))
        if not user.get("deleted")
    ]
    resolved: list[str] = []
    for token in tokens:
        match = _USER_TOKEN.match(token)
        if not match:
            continue
        if match.group(1):
            resolved.append(match.group(1))
            continue
        name = match.group(2).casefold()
        matches = [
            user
            for user in users
            if name
            in {
                str(user.get("name", "")).casefold(),
                str(user.get("real_name", "")).casefold(),
                str(user.get("profile", {}).get("display_name", "")).casefold(),
            }
        ]
        if len(matches) != 1:
            return [], f"I couldn't uniquely identify `{token}`. Use Slack's user mention picker."
        resolved.append(str(matches[0].get("id", "")))
    return [user for user in dict.fromkeys(resolved) if user], None


async def _resolve_channel_tokens(client: Any, tokens: list[str]) -> tuple[list[str], str | None]:
    if not tokens:
        return [], None
    canonical_ids = [_CHANNEL_TOKEN.match(token) for token in tokens]
    if all(match is not None and match.group(1) for match in canonical_ids):
        return [str(match.group(1)) for match in canonical_ids if match is not None], None
    response = await client.conversations_list(
        types="public_channel,private_channel", exclude_archived=True, limit=200
    )
    channels = [
        channel
        for channel in response.get("channels", [])
        if not channel.get("is_archived")
    ]
    resolved: list[str] = []
    for token in tokens:
        match = _CHANNEL_TOKEN.match(token)
        if not match:
            continue
        if match.group(1):
            resolved.append(match.group(1))
            continue
        name = match.group(2).casefold()
        matches = [
            channel for channel in channels if str(channel.get("name", "")).casefold() == name
        ]
        if len(matches) != 1:
            return [], (
                f"I couldn't uniquely identify `{token}`. "
                "Choose the channel from Slack's picker."
            )
        resolved.append(str(matches[0].get("id", "")))
    return [channel for channel in dict.fromkeys(resolved) if channel], None


def _quick_submit_view(
    metadata: dict[str, Any],
    *,
    candidates: list[str],
    scope: str,
    channel_ids: list[str],
    message_values: dict[str, Any],
    retention_hours: int,
) -> dict[str, Any]:
    """Adapt a compact modal's state to the regular message submission shape."""
    return {
        "private_metadata": json.dumps(
            {
                **metadata,
                "candidates": candidates,
                "scope": scope,
                "channel_ids": channel_ids,
            },
            separators=(",", ":"),
        ),
        "state": {
            "values": {
                "message": message_values,
                "retention_amount": {
                    "retention_amount_input": {"value": str(retention_hours)}
                },
                "retention_unit": {
                    "retention_unit_choice": {
                        "selected_option": {"value": "hours"}
                    }
                },
            }
        },
    }


def _target_reference(target_id: str) -> str:
    """Return a safe mention when a target exists, or neutral channel wording."""
    return f"<@{target_id}>" if target_id else "the person being sought"


async def _set_ping_expiry(
    repository: ReachRepository, ping_id: str, expires_at: datetime | None
) -> None:
    if expires_at is None:
        return
    setter = getattr(repository, "set_ping_expiry", None)
    if setter is not None:
        await asyncio.to_thread(setter, ping_id, expires_at)


def decode_action_value(value: str) -> dict[str, str]:
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except json.JSONDecodeError:
        pass
    try:
        parsed = ast.literal_eval(value)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except (ValueError, SyntaxError):
        pass
    raise ValueError("Invalid action payload")


def _action_channel_id(body: dict[str, Any]) -> str:
    """Channel of a block_actions interaction, tolerating both payload shapes."""
    channel = str(body.get("channel_id") or "")
    if not channel and isinstance(body.get("channel"), dict):
        channel = str(body["channel"].get("id") or "")
    return channel


def _parse_modal_metadata(raw: str) -> tuple[str, str]:
    """Extract (ping_id, channel_id) from modal private_metadata.

    Modal submissions carry no channel of their own, so the channel is
    threaded through private_metadata at render time (needed for the
    ephemeral acks on broadcast messages). Tolerates the legacy
    plain-ping_id format.
    """
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return str(parsed.get("ping_id", "")), str(parsed.get("channel_id", ""))
    except json.JSONDecodeError:
        pass
    return raw, ""


def _broadcast_closed(ping: Ping) -> bool:
    """True when a broadcast message has already met a local closure threshold."""
    return (
        ping.local_know_count >= BROADCAST_KNOW_LIMIT
        or ping.local_total_count >= BROADCAST_TOTAL_LIMIT
    )


async def _post_ephemeral(client: Any, channel: str, user_id: str, text: str) -> None:
    """Per-responder ephemeral acknowledgment (visible only to that responder)."""
    if not channel or not user_id:
        return
    try:
        await client.chat_postEphemeral(channel=channel, user=user_id, text=text)
    except SlackApiError as exc:
        log.error("chat_postEphemeral failed for channel=%s user=%s: %s", channel, user_id, exc)


async def _delete_message(client: Any, ping: Ping) -> bool:
    if not ping.channel or not ping.message_ts:
        return False
    try:
        delete = getattr(client, "chat_delete", None)
        if delete is None:
            # Compatibility for lightweight clients used by older integrations;
            # the real Slack client always has chat.delete.
            await client.chat_update(channel=ping.channel, ts=ping.message_ts, text=CUTOFF_STATUS)
        else:
            await delete(channel=ping.channel, ts=ping.message_ts)
        return True
    except SlackApiError as exc:
        if exc.response.get("error") in {"message_not_found", "channel_not_found"}:
            return True
        log.error("message deletion failed for ping=%s: %s", ping.id, exc)
        return False


async def delete_expired_messages(repository: ReachRepository, client: Any) -> int:
    """Delete all tracked messages whose configured retention has elapsed."""
    getter = getattr(repository, "get_expired_pings", None)
    marker = getattr(repository, "mark_ping_deleted", None)
    if getter is None or marker is None:
        return 0
    deleted = 0
    for ping in await asyncio.to_thread(getter, datetime.now(UTC)):
        if await _delete_message(client, ping):
            await asyncio.to_thread(marker, ping.id, datetime.now(UTC))
            deleted += 1
    return deleted


async def delete_legacy_messages(repository: ReachRepository, client: Any) -> int:
    """One-time cleanup for messages sent before retention was introduced."""
    getter = getattr(repository, "get_pings_before", None)
    marker = getattr(repository, "mark_ping_deleted", None)
    if getter is None or marker is None:
        return 0
    deleted = 0
    for ping in await asyncio.to_thread(getter, RETENTION_FEATURE_INTRODUCED_AT):
        if await _delete_message(client, ping):
            await asyncio.to_thread(marker, ping.id, datetime.now(UTC))
            deleted += 1
    return deleted


async def _finish_broadcast_response(
    repository: ReachRepository,
    client: Any,
    ping: Ping,
    responder_id: str,
    channel: str,
    counts_toward_known: bool,
) -> None:
    """Apply local counters, acknowledge, and perform one closure update."""
    know, total, closed_now = repository.increment_broadcast_counts(
        ping.id, counts_toward_known
    )
    log.info(
        "broadcast response ping=%s responder=%s know_count=%s total_count=%s closed_now=%s",
        ping.id,
        responder_id,
        know,
        total,
        closed_now,
    )
    await _post_ephemeral(
        client,
        channel or ping.channel,
        responder_id,
        CUTOFF_STATUS if closed_now else "Thanks for your response!",
    )
    if not closed_now or not ping.message_ts:
        return
    # Broadcast posts are shared by an entire channel. Once the local
    # threshold is reached, remove the bot's post instead of leaving a
    # visible "Someone already..." status message in the channel. The
    # triggering responder still gets the status ephemerally above.
    if await _delete_message(client, ping):
        marker = getattr(repository, "mark_ping_deleted", None)
        if marker is not None:
            marker(ping.id, datetime.now(UTC))
    try:
        await client.chat_postMessage(
            channel=ping.requester_id,
            text=(
                f"The broadcast in <#{ping.channel}> closed after {know} \"I know\" "
                f"response(s) and {total} total response(s)."
            ),
        )
    except SlackApiError as exc:
        log.error("broadcast closure notification failed for ping=%s: %s", ping.id, exc)


def register_handlers(
    slack_app: Any,
    *,
    repository: ReachRepository,
    known_response_limit: int = 3,
    quick_reach_retention_hours: int = 2,
) -> None:
    @slack_app.command("/reach")  # type: ignore[untyped-decorator]
    async def reach_command(
        ack: Callable[..., Awaitable[None]],
        command: dict[str, Any],
        respond: Callable[..., Awaitable[None]],
        client: Any,
    ) -> None:
        # ack() must be the very first thing we do — Slack gives a 3-second
        # window to acknowledge the command, and anything before this call
        # (including the views_open network request below) eats into it.
        await ack()
        try:
            text = str(command.get("text", "")).strip()
            user_tokens, channel_tokens = _shortcut_tokens(text)
            if len(user_tokens) > 1:
                await respond(
                    response_type="ephemeral",
                    text="Use at most one target person, for example `/reach @username #channel`.",
                )
                return
            if text and not user_tokens and not channel_tokens:
                await respond(
                    response_type="ephemeral",
                    text="Use `/reach`, `/reach @username`, `/reach #channel`, or both together.",
                )
                return
            target_ids, user_error = await _resolve_user_tokens(client, user_tokens)
            channel_ids, channel_error = await _resolve_channel_tokens(client, channel_tokens)
            if user_error or channel_error:
                await respond(response_type="ephemeral", text=user_error or channel_error)
                return
            requester_id = str(command.get("user_id", ""))
            if target_ids and not channel_ids:
                view = render_quick_people_modal(target_ids[0], requester_id=requester_id)
            elif channel_ids:
                view = render_quick_channels_modal(
                    requester_id=requester_id,
                    target_id=target_ids[0] if target_ids else "",
                    initial_channels=channel_ids,
                )
            else:
                view = render_reach_stage1()
            await client.views_open(trigger_id=command["trigger_id"], view=view)
        except SlackApiError as exc:
            log.error("/reach Slack API error: %s", exc)
            await respond(
                response_type="ephemeral", text=f"Slack API error: {exc.response['error']}"
            )
        except Exception as exc:
            log.exception("/reach unhandled error for user=%s", command.get("user_id"))
            await respond(response_type="ephemeral", text=f"Something went wrong: {exc}")

    @slack_app.view("reach_quick_people_submit")  # type: ignore[untyped-decorator]
    async def reach_quick_people_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        """Adapt the compact person shortcut into the normal send pipeline."""
        metadata = json.loads(view.get("private_metadata", "{}"))
        values = view["state"]["values"]
        candidates = values.get("candidates", {}).get("candidates", {}).get("selected_users", [])
        if not candidates:
            await ack(response_action="errors", errors={"candidates": "Pick at least one person."})
            return
        await reach_message_submit(
            ack=ack,
            body=body,
            view=_quick_submit_view(
                metadata,
                candidates=[str(candidate) for candidate in candidates],
                scope="none",
                channel_ids=[],
                message_values=values.get("message", {}),
                retention_hours=quick_reach_retention_hours,
            ),
            client=client,
        )

    @slack_app.view("reach_quick_channels_submit")  # type: ignore[untyped-decorator]
    async def reach_quick_channels_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        """Adapt the compact channel shortcut into the normal send pipeline."""
        metadata = json.loads(view.get("private_metadata", "{}"))
        values = view["state"]["values"]
        channel_ids = values.get("broadcast_channels", {}).get("channels_choice", {}).get(
            "selected_conversations", []
        )
        if not channel_ids:
            await ack(
                response_action="errors",
                errors={"broadcast_channels": "Pick at least one channel."},
            )
            return
        await reach_message_submit(
            ack=ack,
            body=body,
            view=_quick_submit_view(
                metadata,
                candidates=[],
                scope="channel",
                channel_ids=[str(channel) for channel in channel_ids],
                message_values=values.get("message", {}),
                retention_hours=quick_reach_retention_hours,
            ),
            client=client,
        )

    @slack_app.command("/reach-cleanup")  # type: ignore[untyped-decorator]
    async def reach_cleanup_command(
        ack: Callable[..., Awaitable[None]],
        command: dict[str, Any],
        respond: Callable[..., Awaitable[None]],
        client: Any,
    ) -> None:
        """Delete tracked pre-retention messages after an explicit confirmation."""
        await ack()
        if str(command.get("text", "")).strip().lower() != "confirm":
            await respond(
                response_type="ephemeral",
                text="This removes all tracked pre-retention Reach messages. "
                "Run `/reach-cleanup confirm` to proceed.",
            )
            return
        deleted = await delete_legacy_messages(repository, client)
        await respond(
            response_type="ephemeral",
            text=f"Removed {deleted} older Reach message(s) tracked by the bot.",
        )

    @slack_app.view("reach_stage1_submit")  # type: ignore[untyped-decorator]
    async def reach_stage1_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        """Handle Stage 1 submission (target user selection) and transition to Stage 2."""
        values = view["state"]["values"]
        target_id = str(values["target"]["target_user"].get("selected_user", "")).strip()
        if not target_id:
            await ack(
                response_action="errors",
                errors={"target": "Please select who you are trying to reach."},
            )
            return
        requester_id = str(body["user"]["id"])
        # One fast users_info call resolves the display name for the message
        # prefill; everything else in Stage 2 is static, so the modal swaps
        # directly inside this ack -- no loading view and no ranking step
        # (suggestions are dormant).
        target_name = target_id
        try:
            info = await client.users_info(user=target_id)
            profile = info.get("user", {}).get("profile", {})
            target_name = str(
                profile.get("display_name")
                or info.get("user", {}).get("real_name")
                or info.get("user", {}).get("name")
                or target_id
            )
        except SlackApiError as exc:
            log.warning("users_info failed for target=%s: %s", target_id, exc)
        await ack(
            response_action="update",
            view=render_reach_audience(
                target_id, requester_id=requester_id, target_name=target_name
            ),
        )

    @slack_app.view("reach_audience_submit")  # type: ignore[untyped-decorator]
    async def reach_audience_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        """Validate the audience, then open the message stage."""
        values = view["state"]["values"]
        metadata = json.loads(view.get("private_metadata", "{}"))
        target_id = str(metadata.get("target_id", "")).strip()
        if not target_id:
            log.warning("reach_audience_submit received a view without a target")
            await ack(
                response_action="errors",
                errors={"candidates": "Session expired. Restart /reach."},
            )
            return
        requester_id = str(metadata.get("requester_id") or body["user"]["id"])
        candidates = [
            str(user)
            for user in dict.fromkeys(
                values.get("candidates", {}).get("candidates", {}).get("selected_users", [])
            )
        ]
        scope = str(
            (
                values.get("broadcast_scope", {}).get("scope_choice", {}).get("selected_option")
                or {}
            ).get("value", "none")
        )
        channel_id = str(
            values.get("broadcast_channel", {})
            .get("channel_choice", {})
            .get("selected_conversation", "")
            or ""
        )
        if not candidates and scope == "none":
            await ack(
                response_action="errors",
                errors={
                    "candidates": "Pick at least one person, or choose a broadcast option below."
                },
            )
            return
        if scope == "channel" and not channel_id:
            await ack(response_action="errors", errors={"broadcast_channel": "Pick a channel."})
            return
        await ack(
            response_action="push",
            view=render_reach_message(
                target_id,
                requester_id=requester_id,
                target_name=str(metadata.get("target_name", "")) or None,
                candidates=candidates,
                scope=scope,
                channel_id=channel_id,
            ),
        )

    @slack_app.view("reach_message_submit")  # type: ignore[untyped-decorator]
    async def reach_message_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        """Send the composed message after the audience stage is complete."""
        values = view["state"]["values"]
        metadata = json.loads(view.get("private_metadata", "{}"))
        target_id = str(metadata.get("target_id", "")).strip()
        requester_id = str(metadata.get("requester_id") or body["user"]["id"])
        candidates = [str(user) for user in dict.fromkeys(metadata.get("candidates", []))]
        scope = str(metadata.get("scope", "none"))
        if not target_id and scope != "channel":
            await ack(
                response_action="errors",
                errors={"message": "Session expired. Restart /reach."},
            )
            return
        message = str(values.get("message", {}).get("message_input", {}).get("value", "")).strip()
        if not message:
            await ack(response_action="errors", errors={"message": "Enter a message to send."})
            return
        expires_at, retention_error = _retention_expiry(values)
        if retention_error:
            await ack(response_action="errors", errors={"retention_amount": retention_error})
            return
        # Ack immediately before the slow DB writes and chat_postMessage calls.
        await ack()
        request = await asyncio.to_thread(repository.create_reach_request, requester_id, target_id)
        hand_picked = [user for user in candidates if user not in {target_id, requester_id}]
        text_hand_picked = (
            f"Yo, Reach'em here, <@{requester_id}> needs a quick talk with  "
            f"<@{target_id}> (s)He says:\n{message}"
        )
        for candidate_id in hand_picked:
            # Suggestions are dormant: no computed channel context or
            # presence exists for hand-picked recipients.
            ping = await asyncio.to_thread(
                repository.create_ping,
                request.id,
                requester_id,
                target_id,
                candidate_id,
                None,
                "unknown",
            )
            # Mentions are built server-side from stored IDs; the requester's
            # composed message rides along as free text (modal input is
            # literal text Slack never resolves into mentions).
            text = text_hand_picked
            response = await client.chat_postMessage(
                channel=candidate_id,
                text=text,
                blocks=[
                    mrkdwn_section(text),
                    render_recipient_actions(
                        ping_id=ping.id,
                        reach_request_id=request.id,
                        requester_id=requester_id,
                        target_id=target_id,
                        candidate_id=candidate_id,
                    ),
                ],
            )
            # Remember where the message landed so it can be rewritten when
            # the response cutoff is reached.
            await asyncio.to_thread(
                repository.set_ping_delivery,
                ping.id,
                str(response.get("channel", "")),
                str(response.get("ts", "")),
            )
            await _set_ping_expiry(repository, ping.id, expires_at)
        if scope in {"workspace", "channel"}:
            # Workspace scope resolves all public workspace channels inside
            # post_to_channels. Channel scope posts only to the one channel
            # selected in the audience stage.
            task = asyncio.create_task(
                post_to_channels(
                    client,
                    repository,
                    reach_request_id=request.id,
                    requester_id=requester_id,
                    target_id=target_id,
                    scope=scope,
                    channel_ids=[str(channel) for channel in metadata.get("channel_ids", [])]
                    if scope == "channel"
                    else [],
                    message=message,
                    known_response_limit=known_response_limit,
                    expires_at=expires_at,
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

    @slack_app.action("scope_choice")  # type: ignore[untyped-decorator]
    async def scope_choice_changed(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], client: Any
    ) -> None:
        """Show or hide the channel picker as the broadcast scope changes."""
        await ack()
        view = body["view"]
        state = view["state"]["values"]
        metadata = json.loads(view.get("private_metadata", "{}"))
        scope = str(
            (
                state.get("broadcast_scope", {}).get("scope_choice", {}).get("selected_option")
                or {}
            ).get("value", "none")
        )
        selected_users = [
            str(user)
            for user in state.get("candidates", {}).get("candidates", {}).get("selected_users", [])
        ]
        selected_channel = str(
            state.get("broadcast_channel", {})
            .get("channel_choice", {})
            .get("selected_conversation", "")
            or ""
        )
        await client.views_update(
            view_id=view["id"],
            view=render_reach_audience(
                str(metadata.get("target_id", "")),
                requester_id=str(metadata.get("requester_id", "")),
                target_name=str(metadata.get("target_name", "")) or None,
                scope=scope,
                initial_candidates=selected_users,
                initial_channel=selected_channel if scope == "channel" else None,
            ),
        )

    @slack_app.action(re.compile(r"^ping_candidate(?:_|$)"))  # type: ignore[untyped-decorator]
    async def ping_candidate(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], client: Any
    ) -> None:
        await ack()
        value = decode_action_value(body["actions"][0]["value"])
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=render_ping_modal(
                value["candidate_id"], value["target_id"], ping_id=value.get("ping_id", "")
            ),
        )

    @slack_app.view("ping_submit")  # type: ignore[untyped-decorator]
    async def ping_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        await ack()
        candidate_id, target_id, ping_id = view["private_metadata"].split(":", 2)
        message = view["state"]["values"]["message"]["message_input"]["value"]
        ping = repository.get_ping(ping_id)
        text = f"<@{body['user']['id']}> is trying to reach you about <@{target_id}>:\n{message}"
        response = await client.chat_postMessage(
            channel=candidate_id,
            text=text,
            blocks=[
                mrkdwn_section(text),
                render_recipient_actions(
                    ping_id=ping_id,
                    reach_request_id=ping.reach_request_id if ping else "",
                    requester_id=ping.requester_id if ping else str(body["user"]["id"]),
                    target_id=ping.target_id if ping else target_id,
                    candidate_id=ping.candidate_id if ping else candidate_id,
                ),
            ],
        )
        if ping is not None and ping_id:
            repository.set_ping_delivery(
                ping_id, str(response.get("channel", "")), str(response.get("ts", ""))
            )

    @slack_app.action("outcome_helped")  # type: ignore[untyped-decorator]
    async def outcome_helped(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], client: Any
    ) -> None:
        """Opens the one-field location modal before recording anything."""
        await ack()
        value = decode_action_value(body["actions"][0].get("value", "{}"))
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=render_location_modal(
                value.get("ping_id", ""), channel_id=_action_channel_id(body)
            ),
        )

    @slack_app.action("outcome_unknown")  # type: ignore[untyped-decorator]
    async def outcome_unknown(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], client: Any
    ) -> None:
        await ack()
        action = body.get("actions", [{}])[0]
        value = decode_action_value(action.get("value", "{}"))
        ping_id = value.get("ping_id")
        if not ping_id:
            return
        ping = repository.get_ping(ping_id)
        if ping is None:
            log.warning("outcome_unknown received unknown ping_id=%s", ping_id)
            return
        is_broadcast = ping.candidate_id == ""
        responder_id = str(body.get("user", {}).get("id", ""))
        channel = _action_channel_id(body) if is_broadcast else ""
        # Duplicate-click guard is per responder for a shared broadcast post.
        if not is_broadcast and repository.get_outcome(ping_id) is not None:
            if is_broadcast:
                # Closed-message (or duplicate) click: friendly ephemeral
                # note only — no recording, no re-update.
                await _post_ephemeral(
                    client, channel or ping.channel, responder_id, BROADCAST_CLOSED_NOTE
                )
            else:
                try:
                    await client.chat_postMessage(
                        channel=ping.candidate_id,
                        text="You've already responded to this — thanks!",
                    )
                except SlackApiError as exc:
                    log.error("ephemeral note failed for ping=%s: %s", ping_id, exc)
            return
        if is_broadcast:
            if not repository.record_broadcast_response(
                ping_id, responder_id, PingOutcome(ping_id, "unknown")
            ):
                await _post_ephemeral(
                    client, channel or ping.channel, responder_id, BROADCAST_CLOSED_NOTE
                )
                return
            await _finish_broadcast_response(
                repository, client, ping, responder_id, channel, False
            )
        else:
            # Pool 1 (manual DM): unchanged in-place thank-you cleanup.
            repository.record_outcome(PingOutcome(ping_id, "unknown"))
            await _apply_response_cleanup(client, ping)

    @slack_app.action("outcome_more")  # type: ignore[untyped-decorator]
    async def outcome_more(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], client: Any
    ) -> None:
        await ack()
        value = decode_action_value(body["actions"][0].get("value", "{}"))
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=render_reply_modal(
                value["ping_id"], channel_id=_action_channel_id(body)
            ),
        )

    @slack_app.view("reply_more_submit")  # type: ignore[untyped-decorator]
    async def reply_more_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        await ack()
        ping_id, modal_channel = _parse_modal_metadata(str(view["private_metadata"]))
        reply = str(view["state"]["values"]["reply"]["reply_input"].get("value", "")).strip()
        ping = repository.get_ping(ping_id)
        if ping is None:
            log.warning("reply_more received unknown ping_id=%s", ping_id)
            return
        is_broadcast = ping.candidate_id == ""
        responder_id = str(body.get("user", {}).get("id", "unknown"))
        # Duplicate-click guard is per responder for a shared broadcast post.
        if not is_broadcast and repository.get_outcome(ping_id) is not None:
            if is_broadcast:
                await _post_ephemeral(
                    client, modal_channel or ping.channel, responder_id, BROADCAST_CLOSED_NOTE
                )
            else:
                try:
                    await client.chat_postMessage(
                        channel=ping.candidate_id,
                        text="You've already responded to this — thanks!",
                    )
                except SlackApiError as exc:
                    log.error("ephemeral note failed for ping=%s: %s", ping_id, exc)
            return
        if is_broadcast:
            if not repository.record_broadcast_response(
                ping_id, responder_id, PingOutcome(ping_id, "replied")
            ):
                await _post_ephemeral(
                    client, modal_channel or ping.channel, responder_id, BROADCAST_CLOSED_NOTE
                )
                return
            await client.chat_postMessage(
                channel=ping.requester_id,
                text=(
                    f"Reply about {_target_reference(ping.target_id)} "
                    f"from <@{responder_id}>:\n{reply}"
                ),
            )
            await _finish_broadcast_response(
                repository, client, ping, responder_id, modal_channel, False
            )
        else:
            # Manual DM: unchanged in-place thank-you cleanup.
            repository.record_outcome(PingOutcome(ping_id, "replied"))
            await client.chat_postMessage(
                channel=ping.requester_id,
                text=(
                    f"Reply about {_target_reference(ping.target_id)} "
                    f"from <@{responder_id}>:\n{reply}"
                ),
            )
            await _apply_response_cleanup(client, ping)

    @slack_app.view("location_submit")  # type: ignore[untyped-decorator]
    async def location_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        """Handle the "I know" modal: record, relay, and enforce the right pool's cutoff."""
        await ack()
        ping_id, modal_channel = _parse_modal_metadata(str(view.get("private_metadata", "")))
        location = str(
            view["state"]["values"]["location"]["location_input"].get("value", "")
        ).strip()
        ping = repository.get_ping(ping_id) if ping_id else None
        if ping is None:
            log.warning("location_submit received unknown ping_id=%s", ping_id)
            return
        is_broadcast = ping.candidate_id == ""
        responder_id = str(body.get("user", {}).get("id", ""))
        # Re-check on submit: the button may have been clicked before a
        # cutoff that has since been reached, and a ping that already
        # answered must not be counted or recorded twice.
        if not is_broadcast and repository.get_outcome(ping_id) is not None:
            if is_broadcast:
                # Closed broadcast message: friendly ephemeral note only —
                # never a visible chat.update (the status line, if any, was
                # already applied by the closing response).
                await _post_ephemeral(
                    client, modal_channel or ping.channel, responder_id, BROADCAST_CLOSED_NOTE
                )
            else:
                await _replace_with_cutoff_status(client, ping)
            return
        if is_broadcast:
            broadcast_outcome = PingOutcome(
                ping_id, "helped", responded_at=datetime.now(UTC), location=location or None
            )
            if not repository.record_broadcast_response(ping_id, responder_id, broadcast_outcome):
                await _post_ephemeral(
                    client, modal_channel or ping.channel, responder_id, BROADCAST_CLOSED_NOTE
                )
                return
            if location:
                await client.chat_postMessage(
                    channel=ping.requester_id,
                    text=(
                        f"<@{responder_id}> knows how to reach "
                        f"{_target_reference(ping.target_id)} (s)He says:\n {location}"
                    ),
                )
            # Ephemeral ack to the responder; broadcast messages are never
            # edited in place for individual responders (many people share
            # the same message).
            await _finish_broadcast_response(
                repository, client, ping, responder_id, modal_channel, True
            )
            return
        # Pool 1 (manual/hand-picked DM): global hunt-wide counter, unchanged.
        count = repository.increment_known_count(ping.reach_request_id)
        if count > known_response_limit:
            await _replace_with_cutoff_status(client, ping)
            return
        repository.record_outcome(
            PingOutcome(
                ping_id,
                "helped",
                responded_at=datetime.now(UTC),
                location=location or None,
            )
        )
        if location and ping.candidate_id:
            await client.chat_postMessage(
                channel=ping.requester_id,
                text=(
                    f"<@{ping.candidate_id}> knows how to reach "
                    f"{_target_reference(ping.target_id)} (s)He says:\n {location}"
                ),
            )
        await _apply_response_cleanup(client, ping)
        if count == known_response_limit:
            # DM-only sweep: broadcast pings are excluded at the data layer
            # (get_unresponded_pings), so this can never close them.
            await _sweep_open_messages(repository, client, ping)

    @slack_app.view("reach_stage3_submit")  # type: ignore[untyped-decorator]
    async def reach_stage3_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        """Stage 3 submission: selected channels + message → send broadcast posts."""
        values = view["state"]["values"]
        metadata = json.loads(view.get("private_metadata", "{}"))
        target_id = str(metadata.get("target_id", "")).strip()
        requester_id = str(metadata.get("requester_id", body.get("user", {}).get("id"))).strip()
        if not target_id or not requester_id:
            await ack(
                response_action="errors", errors={"message": "Session missing. Restart /reach."}
            )
            return

        selected_conversations = (
            values.get("broadcast_channels", {})
            .get("channels_choice", {})
            .get("selected_conversations", [])
        )
        channel_ids = [str(ch) for ch in (selected_conversations or []) if ch]
        if not channel_ids:
            await ack(
                response_action="errors",
                errors={"broadcast_channels": "Pick at least one public channel."},
            )
            return

        message = str(values.get("message", {}).get("message_input", {}).get("value", "")).strip()
        if not message:
            await ack(response_action="errors", errors={"message": "Enter a message to send."})
            return
        expires_at, retention_error = _retention_expiry(values)
        if retention_error:
            await ack(response_action="errors", errors={"retention_amount": retention_error})
            return

        await ack()
        request = await asyncio.to_thread(repository.create_reach_request, requester_id, target_id)

        # Send to selected public channels via bounded concurrent post_to_channels.
        task = asyncio.create_task(
            post_to_channels(
                client,
                repository,
                reach_request_id=request.id,
                requester_id=requester_id,
                target_id=target_id,
                scope="channel",
                channel_ids=channel_ids,
                message=message,
                known_response_limit=known_response_limit,
                expires_at=expires_at,
            )
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


async def _replace_with_cutoff_status(client: Any, ping: Ping) -> None:
    """Swap a stale recipient message's buttons for the cutoff status line."""
    if not ping.message_ts:
        return
    try:
        await client.chat_update(channel=ping.channel, ts=ping.message_ts, text=CUTOFF_STATUS)
    except SlackApiError as exc:
        log.error("chat_update cutoff status failed for ping=%s: %s", ping.id, exc)


async def _apply_response_cleanup(client: Any, ping: Ping) -> None:
    """Remove actions block from a manual DM message and show thank-you line.

    Pool 1 (manual/hand-picked DM recipients) ONLY: this is the per-responder
    in-place thank-you edit on the responder's own DM message
    (ping.channel = DM channel). Broadcast messages share one post per
    channel, so they must never be edited here — their closure is handled
    exclusively by their local thresholds (Pool 2), and their responders get
    an ephemeral ack instead.
    """
    if not ping.message_ts or not ping.channel:
        return
    try:
        await client.chat_update(channel=ping.channel, ts=ping.message_ts, text=THANK_YOU_STATUS)
    except SlackApiError as exc:
        log.error("chat_update cleanup failed for ping=%s: %s", ping.id, exc)


async def _sweep_open_messages(
    repository: ReachRepository, client: Any, responded_ping: Ping
) -> None:
    """Replace the buttons on every other still-open DM message for this request.

    Pool 1 only: broadcast pings are excluded at the data layer
    (get_unresponded_pings), so this global-threshold sweep can never touch
    or close a broadcast message — those are closed exclusively by their own
    local thresholds.
    """
    others = repository.get_unresponded_pings(responded_ping.reach_request_id)
    for other in others:
        if other.id == responded_ping.id or not other.message_ts:
            continue
        try:
            await client.chat_update(channel=other.channel, ts=other.message_ts, text=CUTOFF_STATUS)
        except SlackApiError as exc:
            log.error("chat_update cutoff sweep failed for ping=%s: %s", other.id, exc)
