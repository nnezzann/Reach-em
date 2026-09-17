from __future__ import annotations

import ast
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from reach_bot.conversation import ConversationStore
from reach_bot.nvidia import NvidiaClient, NvidiaClientError
from reach_bot.persistence import PingOutcome, ReachRepository
from reach_bot.rendering import render_ping_modal, render_why

logger = logging.getLogger(__name__)


def is_direct_message_command(command: dict[str, Any]) -> bool:
    """Accept Slack DM payloads even when channel_type is omitted."""
    channel_type = str(command.get("channel_type", "")).lower()
    channel_id = str(command.get("channel_id", ""))
    return channel_type == "im" or channel_id.startswith("D")


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
    assistant: NvidiaClient | None = None,
    conversations: ConversationStore | None = None,
) -> None:
    assistant = assistant or NvidiaClient(None)
    conversations = conversations or ConversationStore()

    async def answer_in_dm(
        *,
        user_id: str,
        channel_id: str,
        text: str,
        client: Any,
        thread_ts: str | None = None,
        request_id: str | None = None,
    ) -> None:
        request_id = request_id or uuid4().hex
        key = f"{user_id}:{channel_id}"
        conversations.add(key, "user", text)
        try:
            response = await assistant.complete(
                [
                    {"role": message.role, "content": message.content}
                    for message in conversations.get(key)
                ]
            )
        except NvidiaClientError as exc:
            logger.warning(
                "assistant response failed request_id=%s category=%s",
                request_id,
                type(exc).__name__,
            )
            await client.chat_postMessage(
                channel=channel_id,
                text=str(exc),
                **({"thread_ts": thread_ts} if thread_ts else {}),
            )
            return
        conversations.add(key, "assistant", response)
        try:
            await client.chat_postMessage(
                channel=channel_id,
                text=response,
                **({"thread_ts": thread_ts} if thread_ts else {}),
            )
        except Exception:
            logger.exception("Slack reply failed request_id=%s", request_id)
            raise

    async def safe_answer(**kwargs: Any) -> None:
        try:
            await answer_in_dm(**kwargs)
        except Exception:
            logger.exception("DM assistant handler failed")

    @slack_app.event("message")  # type: ignore[untyped-decorator]
    async def dm_message(event: dict[str, Any], client: Any) -> None:
        if (
            event.get("subtype")
            or event.get("bot_id")
            or not event.get("user")
            or event.get("channel_type") != "im"
            or not str(event.get("text", "")).strip()
        ):
            return
        logger.info("DM message received channel_type=%s", event.get("channel_type"))
        request_id = uuid4().hex
        logger.info("DM assistant request started request_id=%s", request_id)
        asyncio.create_task(
            safe_answer(
                user_id=str(event["user"]),
                channel_id=str(event["channel"]),
                text=str(event["text"]).strip(),
                client=client,
                thread_ts=str(event.get("thread_ts") or event.get("ts") or "") or None,
                request_id=request_id,
            )
        )

    @slack_app.command("/reach")  # type: ignore[untyped-decorator]
    async def reach_command(
        ack: Callable[..., Awaitable[None]],
        command: dict[str, Any],
        respond: Callable[..., Awaitable[None]],
        client: Any,
    ) -> None:
        try:
            await ack()
        except Exception:
            logger.exception("Slack slash command acknowledgement failed")
            raise
        text = str(command.get("text", "")).strip()
        request_id = uuid4().hex
        logger.info(
            "slash command received request_id=%s channel_type=%s",
            request_id,
            command.get("channel_type"),
        )
        if not is_direct_message_command(command):
            await respond(
                response_type="ephemeral",
                text=(
                    "Please open Reach's Messages tab and use `/reach` there. "
                    "Slack slash commands cannot be used inside assistant threads."
                ),
            )
            return
        if not text:
            await client.chat_postMessage(
                channel=command["channel_id"],
                text=(
                    "Tell me what you need help with, for example: "
                    "`/reach find the deploy owner`."
                ),
            )
            return
        await answer_in_dm(
            user_id=str(command["user_id"]),
            channel_id=str(command["channel_id"]),
            text=text,
            client=client,
            request_id=request_id,
        )

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
