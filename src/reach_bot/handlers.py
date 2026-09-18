from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from slack_sdk.errors import SlackApiError

from reach_bot.persistence import PingOutcome, ReachRepository
from reach_bot.rendering import (
    render_ping_modal,
    render_reach_stage1,
    render_reach_stage2,
    render_recipient_actions,
    render_reply_modal,
    render_why,
)

log = logging.getLogger(__name__)


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
    ranker: Callable[..., Any],
    renderer: Callable[..., list[dict[str, Any]]],
) -> None:
    @slack_app.command("/reach")  # type: ignore[untyped-decorator]
    async def reach_command(
        ack: Callable[..., Awaitable[None]],
        command: dict[str, Any],
        respond: Callable[..., Awaitable[None]],
        client: Any,
    ) -> None:
        try:
            await client.views_open(trigger_id=command["trigger_id"], view=render_reach_stage1())
            await ack()
        except SlackApiError as exc:
            log.error("/reach Slack API error: %s", exc)
            await ack()
            await respond(
                response_type="ephemeral", text=f"Slack API error: {exc.response['error']}"
            )
        except Exception as exc:
            log.exception("/reach unhandled error for user=%s", command.get("user_id"))
            await ack()
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
        # Ack immediately — Slack enforces a 3-second window and the ranking
        # call makes multiple Slack API requests that will exceed that limit.
        await ack()
        requester_id = str(body["user"]["id"])
        view_id = view["id"]
        try:
            ranked = await asyncio.to_thread(ranker, target_id, requester_id)
            stage2_view = render_reach_stage2(target_id, ranked, requester_id=requester_id)
            await client.views_update(view_id=view_id, view=stage2_view)
        except Exception as exc:
            log.exception("reach_stage1_submit error: %s", exc)
            await client.views_update(
                view_id=view_id,
                view={
                    "type": "modal",
                    "callback_id": "reach_stage1_submit",
                    "title": {"type": "plain_text", "text": "Reach someone"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f":warning: Something went wrong: {exc}",
                            },
                        }
                    ],
                },
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
        suggested: list[str] = []
        for block in values.values():
            element = next(iter(block.values()))
            suggested.extend(str(option["value"]) for option in element.get("selected_options", []))
        manual = values.get("manual_candidates", {}).get("manual_candidates", {})
        recipients = list(dict.fromkeys((*suggested, *manual.get("selected_users", []))))
        recipients = [user_id for user_id in recipients if user_id not in {target_id, requester_id}]
        if not recipients:
            await ack(
                response_action="errors",
                errors={"manual_candidates": "Select at least one recipient."},
            )
            return
        message = str(values["message"]["message"]["message_input"].get("value", "")).strip()
        if not message:
            await ack(
                response_action="errors",
                errors={"message": "Enter a message to send."},
            )
            return
        # Ack immediately before the slow DB writes and chat_postMessage calls.
        await ack()
        request = await asyncio.to_thread(repository.create_reach_request, requester_id, target_id)
        ranked = await asyncio.to_thread(ranker, target_id, requester_id)
        by_id = {candidate.user_id: candidate for candidate in (*ranked.active, *ranked.offline)}
        for candidate_id in recipients:
            candidate = by_id.get(candidate_id)
            ping = await asyncio.to_thread(
                repository.create_ping,
                request.id,
                requester_id,
                target_id,
                candidate_id,
                candidate.evidence[0] if candidate and candidate.evidence else None,
                candidate.presence if candidate else "offline",
            )
            await client.chat_postMessage(
                channel=candidate_id,
                text=f"Reach, relaying for <@{requester_id}>:\n{message}",
                blocks=[
                    render_recipient_actions(
                        ping_id=ping.id,
                        reach_request_id=request.id,
                        requester_id=requester_id,
                        target_id=target_id,
                        candidate_id=candidate_id,
                    )
                ],
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

    @slack_app.action("why_these_people")  # type: ignore[untyped-decorator]
    async def why_these_people(
        ack: Callable[..., Awaitable[None]],
        body: dict[str, Any],
        respond: Callable[..., Awaitable[None]],
    ) -> None:
        await ack()
        target_id = str(body["actions"][0]["value"])
        try:
            ranked = await asyncio.to_thread(ranker, target_id, str(body["user"]["id"]))
            await respond(
                response_type="ephemeral", replace_original=False, blocks=render_why(ranked)
            )
        except Exception as exc:
            log.exception("why_these_people error for target=%s", target_id)
            await respond(
                response_type="ephemeral",
                replace_original=False,
                text=f"Something went wrong: {exc}",
            )

    @slack_app.view("ping_submit")  # type: ignore[untyped-decorator]
    async def ping_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        await ack()
        candidate_id, target_id, ping_id = view["private_metadata"].split(":", 2)
        message = view["state"]["values"]["message"]["message_input"]["value"]
        ping = repository.get_ping(ping_id)
        await client.chat_postMessage(
            channel=candidate_id,
            text=f"<@{body['user']['id']}> is trying to reach you about <@{target_id}>:\n{message}",
            blocks=[
                render_recipient_actions(
                    ping_id=ping_id,
                    reach_request_id=ping.reach_request_id if ping else "",
                    requester_id=ping.requester_id if ping else str(body["user"]["id"]),
                    target_id=ping.target_id if ping else target_id,
                    candidate_id=ping.candidate_id if ping else candidate_id,
                )
            ],
        )

    for action, outcome in (
        ("outcome_helped", "helped"),
        ("outcome_unknown", "unknown"),
    ):

        @slack_app.action(action)  # type: ignore[untyped-decorator]
        async def outcome_handler(
            ack: Callable[..., Awaitable[None]], body: dict[str, Any], _outcome: str = outcome
        ) -> None:
            await ack()
            await _record_outcome(body, repository, _outcome)

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


async def _record_outcome(body: dict[str, Any], repository: ReachRepository, outcome: str) -> None:
    action = body.get("actions", [{}])[0]
    value = decode_action_value(action.get("value", "{}"))
    if "ping_id" in value:
        repository.record_outcome(PingOutcome(value["ping_id"], outcome))


async def _resolve_names(client: Any, user_ids: set[str]) -> dict[str, str]:
    """Look up display names for a small set of user IDs, concurrently.

    Falls back to the raw ID (so button text still renders something
    sensible) if a lookup fails for any reason.
    """

    async def _one(user_id: str) -> tuple[str, str]:
        try:
            info = await client.users_info(user=user_id)
            profile = info.get("user", {}).get("profile", {})
            name = (
                profile.get("display_name")
                or info.get("user", {}).get("real_name")
                or info.get("user", {}).get("name")
                or user_id
            )
            return user_id, str(name)
        except SlackApiError:
            return user_id, user_id

    results = await asyncio.gather(*(_one(uid) for uid in user_ids))
    return dict(results)
