from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from slack_sdk.errors import SlackApiError

from reach_bot.broadcast import fan_out_broadcast
from reach_bot.persistence import Ping, PingOutcome, ReachRepository
from reach_bot.rendering import (
    CUTOFF_STATUS,
    mrkdwn_section,
    render_location_modal,
    render_ping_modal,
    render_reach_stage1,
    render_reach_stage2,
    render_recipient_actions,
    render_reply_modal,
)

log = logging.getLogger(__name__)

# Background fan-out tasks; kept referenced so they are not garbage-collected.
_background_tasks: set[asyncio.Task[Any]] = set()


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


def register_handlers(
    slack_app: Any,
    *,
    repository: ReachRepository,
    known_response_limit: int = 3,
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
            await client.views_open(trigger_id=command["trigger_id"], view=render_reach_stage1())
        except SlackApiError as exc:
            log.error("/reach Slack API error: %s", exc)
            await respond(
                response_type="ephemeral", text=f"Slack API error: {exc.response['error']}"
            )
        except Exception as exc:
            log.exception("/reach unhandled error for user=%s", command.get("user_id"))
            await respond(response_type="ephemeral", text=f"Something went wrong: {exc}")

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
            view=render_reach_stage2(
                target_id, requester_id=requester_id, target_name=target_name
            ),
        )

    @slack_app.view("reach_submit")  # type: ignore[untyped-decorator]
    async def reach_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        """Handle Stage 2 submission (final message to selected candidates)."""
        values = view["state"]["values"]
        metadata = json.loads(view.get("private_metadata", "{}"))
        target_id = str(metadata.get("target_id", "")).strip()
        if not target_id:
            log.warning("reach_submit received a view without a target")
            await ack(
                response_action="errors",
                errors={"message": "Select who you are trying to reach first."},
            )
            return
        requester_id = str(body["user"]["id"])
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
        if scope == "channel" and not channel_id:
            await ack(
                response_action="errors",
                errors={"broadcast_channel": "Pick a channel."},
            )
            return
        if not candidates and scope == "none":
            await ack(
                response_action="errors",
                errors={
                    "candidates": "Pick at least one person, or choose a broadcast option below."
                },
            )
            return
        message = str(values["message"]["message_input"].get("value", "")).strip()
        if not message:
            await ack(
                response_action="errors",
                errors={"message": "Enter a message to send."},
            )
            return
        # Ack immediately before the slow DB writes and chat_postMessage calls.
        await ack()
        request = await asyncio.to_thread(repository.create_reach_request, requester_id, target_id)
        hand_picked = [user for user in candidates if user not in {target_id, requester_id}]
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
            text = f"Reach, relaying for <@{requester_id}> about <@{target_id}>:\n{message}"
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
        if scope in {"channel", "workspace"}:
            task = asyncio.create_task(
                fan_out_broadcast(
                    client,
                    repository,
                    reach_request_id=request.id,
                    requester_id=requester_id,
                    target_id=target_id,
                    scope=scope,
                    channel_id=channel_id,
                    message=message,
                    exclude=set(hand_picked) | {target_id, requester_id},
                    known_response_limit=known_response_limit,
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
        message_value = state.get("message", {}).get("message_input", {}).get("value")
        await client.views_update(
            view_id=view["id"],
            view=render_reach_stage2(
                str(metadata.get("target_id", "")),
                requester_id=str(metadata.get("requester_id", "")),
                target_name=str(metadata.get("target_name", "")) or None,
                scope=scope,
                initial_candidates=selected_users,
                initial_channel=selected_channel if scope == "channel" else None,
                message_value=message_value,
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
            view=render_location_modal(value.get("ping_id", "")),
        )

    @slack_app.action("outcome_unknown")  # type: ignore[untyped-decorator]
    async def outcome_unknown(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any]
    ) -> None:
        await ack()
        await _record_outcome(body, repository, "unknown")

    @slack_app.action("outcome_more")  # type: ignore[untyped-decorator]
    async def outcome_more(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], client: Any
    ) -> None:
        await ack()
        value = decode_action_value(body["actions"][0].get("value", "{}"))
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=render_reply_modal(value["ping_id"]),
        )

    @slack_app.view("reply_more_submit")  # type: ignore[untyped-decorator]
    async def reply_more_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        await ack()
        ping_id = str(view["private_metadata"])
        reply = str(view["state"]["values"]["reply"]["reply_input"].get("value", "")).strip()
        ping = repository.get_ping(ping_id)
        if ping is None:
            log.warning("reply_more received unknown ping_id=%s", ping_id)
            return
        repository.record_outcome(PingOutcome(ping_id, "replied"))
        await client.chat_postMessage(
            channel=ping.requester_id,
            text=f"Reply from Reach recipient:\n{reply}",
        )

    @slack_app.view("location_submit")  # type: ignore[untyped-decorator]
    async def location_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        """Handle the "I know" modal: record, relay, and enforce the cutoff."""
        await ack()
        ping_id = str(view.get("private_metadata", ""))
        location = str(
            view["state"]["values"]["location"]["location_input"].get("value", "")
        ).strip()
        ping = repository.get_ping(ping_id) if ping_id else None
        if ping is None:
            log.warning("location_submit received unknown ping_id=%s", ping_id)
            return
        # Re-check on submit: the button may have been clicked before a
        # cutoff that has since been reached, and a ping that already
        # answered must not be counted or recorded twice.
        if repository.get_outcome(ping_id) is not None:
            await _replace_with_cutoff_status(client, ping)
            return
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
        if location:
            await client.chat_postMessage(
                channel=ping.requester_id,
                text=(
                    f"<@{ping.candidate_id}> knows how to reach "
                    f"<@{ping.target_id}>: {location}"
                ),
            )
        if count == known_response_limit:
            await _sweep_open_messages(repository, client, ping)


async def _replace_with_cutoff_status(client: Any, ping: Ping) -> None:
    """Swap a stale recipient message's buttons for the cutoff status line."""
    if not ping.message_ts:
        return
    try:
        await client.chat_update(channel=ping.channel, ts=ping.message_ts, text=CUTOFF_STATUS)
    except SlackApiError as exc:
        log.error("chat_update cutoff status failed for ping=%s: %s", ping.id, exc)


async def _sweep_open_messages(
    repository: ReachRepository, client: Any, responded_ping: Ping
) -> None:
    """Replace the buttons on every other open message for this reach request."""
    others = repository.get_unresponded_pings(responded_ping.reach_request_id)
    for other in others:
        if other.id == responded_ping.id or not other.message_ts:
            continue
        try:
            await client.chat_update(channel=other.channel, ts=other.message_ts, text=CUTOFF_STATUS)
        except SlackApiError as exc:
            log.error("chat_update cutoff sweep failed for ping=%s: %s", other.id, exc)


async def _record_outcome(body: dict[str, Any], repository: ReachRepository, outcome: str) -> None:
    action = body.get("actions", [{}])[0]
    value = decode_action_value(action.get("value", "{}"))
    if "ping_id" in value:
        repository.record_outcome(PingOutcome(value["ping_id"], outcome))
