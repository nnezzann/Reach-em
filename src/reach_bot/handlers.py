from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)

from reach_bot.persistence import PingOutcome, ReachRepository
from reach_bot.rendering import render_ping_modal, render_why


def parse_command(text: str) -> tuple[str, str | None]:
    parts = text.strip().split(maxsplit=1)
    if not parts:
        raise ValueError("A target user is required")
    # Handle both raw @username and Slack's encoded <@U123ABC> / <@U123ABC|name> formats
    raw = parts[0].strip()
    if raw.startswith("<@") and raw.endswith(">"):
        inner = raw[2:-1]  # strip <@ and >
        target = inner.split("|")[0]  # U123ABC|name -> U123ABC
    else:
        target = raw.lstrip("@")
    if not target or not target.replace("-", "").isalnum():
        raise ValueError(f"Could not parse a valid user from: {raw!r}")
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
        client: Any,
    ) -> None:
        await ack()
        try:
            target, note = parse_command(command.get("text", ""))
            ranked = await asyncio.to_thread(ranker, target, command["user_id"])
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
            # Button text only supports plain_text, which Slack never
            # resolves <@Uxxxx> mentions inside (unlike section/mrkdwn
            # blocks, where mentions render as real names automatically).
            # Resolve each candidate's display name up front so button
            # labels read as "Ping Hashim" instead of "Ping <@U0C1...>".
            names = await _resolve_names(client, {c.user_id for c in shown})
            await respond(
                response_type="ephemeral",
                blocks=renderer(target, ranked, note, ping_ids, names),
            )
        except ValueError as exc:
            log.warning("/reach parse error: %s", exc)
            await respond(response_type="ephemeral", text=str(exc))
        except SlackApiError as exc:
            log.error("/reach Slack API error: %s", exc)
            await respond(response_type="ephemeral", text=f"Slack API error: {exc.response['error']}")
        except Exception as exc:
            log.exception("/reach unhandled error for user=%s", command.get("user_id"))
            await respond(response_type="ephemeral", text=f"Something went wrong: {exc}")

    @slack_app.action(re.compile(r"^ping_candidate_"))  # type: ignore[untyped-decorator]
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
            await respond(response_type="ephemeral", replace_original=False, blocks=render_why(ranked))
        except Exception as exc:
            log.exception("why_these_people error for target=%s", target_id)
            await respond(response_type="ephemeral", replace_original=False, text=f"Something went wrong: {exc}")

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
