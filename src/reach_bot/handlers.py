from __future__ import annotations

import ast
import json
from collections.abc import Awaitable, Callable
from typing import Any

from slack_sdk.errors import SlackApiError

from reach_bot.persistence import PingOutcome, ReachRepository
from reach_bot.rendering import render_ping_modal, render_why


def parse_command(text: str) -> tuple[str, str | None]:
    parts = text.strip().split(maxsplit=1)
    if not parts:
        raise ValueError("A target user is required")
    target = parts[0].strip().strip("<@>")
    if not target or not target.replace("-", "").isalnum():
        raise ValueError("Invalid target user")
    return target, parts[1].strip() if len(parts) > 1 and parts[1].strip() else None


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
    ) -> None:
        await ack()
        try:
            target, note = parse_command(command.get("text", ""))
            ranked = ranker(target, command["user_id"])
            shown = (*ranked.active, *ranked.offline)
            ping_ids = {
                candidate.user_id: repository.create_ping(
                    command["user_id"],
                    target,
                    candidate.user_id,
                    candidate.evidence[0] if candidate.evidence else None,
                    candidate.presence,
                ).id
                for candidate in shown
            }
            await respond(
                response_type="ephemeral",
                blocks=renderer(target, ranked, note, ping_ids),
            )
        except (ValueError, SlackApiError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "Slack could not find that user."
            await respond(response_type="ephemeral", text=message)

    @slack_app.action("ping_candidate")  # type: ignore[untyped-decorator]
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
        ranked = ranker(target_id, str(body["user"]["id"]))
        await respond(response_type="ephemeral", replace_original=False, blocks=render_why(ranked))

    @slack_app.view("ping_submit")  # type: ignore[untyped-decorator]
    async def ping_submit(
        ack: Callable[..., Awaitable[None]], body: dict[str, Any], view: dict[str, Any], client: Any
    ) -> None:
        await ack()
        candidate_id, target_id, ping_id = view["private_metadata"].split(":", 2)
        message = view["state"]["values"]["message"]["message_input"]["value"]
        await client.chat_postMessage(
            channel=candidate_id,
            text=f"<@{body['user']['id']}> is trying to reach you about <@{target_id}>:\n{message}",
            blocks=[
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": action_id,
                            "text": {"type": "plain_text", "text": label},
                            "value": json.dumps({"ping_id": ping_id}),
                        }
                        for action_id, label in (
                            ("outcome_helped", "I'll relay"),
                            ("outcome_relayed", "I know where they are"),
                            ("outcome_unknown", "Don't know"),
                        )
                    ],
                }
            ],
        )

    for action, outcome in (
        ("outcome_helped", "helped"),
        ("outcome_relayed", "relayed"),
        ("outcome_unknown", "unknown"),
    ):

        @slack_app.action(action)  # type: ignore[untyped-decorator]
        async def outcome_handler(
            ack: Callable[..., Awaitable[None]], body: dict[str, Any], _outcome: str = outcome
        ) -> None:
            await ack()
            await _record_outcome(body, repository, _outcome)


async def _record_outcome(body: dict[str, Any], repository: ReachRepository, outcome: str) -> None:
    action = body.get("actions", [{}])[0]
    value = decode_action_value(action.get("value", "{}"))
    if "ping_id" in value:
        repository.record_outcome(PingOutcome(value["ping_id"], outcome))
