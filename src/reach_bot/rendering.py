from __future__ import annotations

import json
from typing import Any

from reach_bot.ranking import Candidate, RankedCandidates

DEFAULT_MESSAGE = "Do you know where they are or how to reach them?"


def render_reach_stage1() -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "reach_stage1",
        "title": {"type": "plain_text", "text": "Reach someone"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "target",
                "label": {"type": "plain_text", "text": "Who are you trying to reach?"},
                "element": {
                    "type": "users_select",
                    "action_id": "target_user",
                    "placeholder": {"type": "plain_text", "text": "Select a person"},
                    "dispatch_action": True,
                },
            }
        ],
    }


def render_reach_stage2(
    target_id: str,
    ranked: RankedCandidates,
    *,
    requester_id: str,
    default_message: str = DEFAULT_MESSAGE,
) -> dict[str, Any]:
    candidates = (*ranked.active, *ranked.offline)
    checked_ids = {candidate.user_id for candidate in candidates[:2]}
    blocks: list[dict[str, Any]] = []
    for label, group in (("Active now", ranked.active), ("Offline", ranked.offline)):
        if not group:
            continue
        options = [
            {
                "text": {"type": "plain_text", "text": candidate.user_id},
                "value": candidate.user_id,
            }
            for candidate in group
        ]
        initial_options = [option for option in options if option["value"] in checked_ids]
        blocks.append(
            {
                "type": "input",
                "block_id": f"suggested_{label.lower().replace(' ', '_')}",
                "label": {"type": "plain_text", "text": label},
                "element": {
                    "type": "checkboxes",
                    "action_id": "suggested_candidates",
                    "options": options,
                    "initial_options": initial_options,
                },
                "optional": True,
            }
        )
    blocks.extend(
        [
            {
                "type": "input",
                "block_id": "manual_candidates",
                "label": {
                    "type": "plain_text",
                    "text": "Add anyone else who might be near them",
                },
                "element": {
                    "type": "multi_users_select",
                    "action_id": "manual_candidates",
                    "placeholder": {"type": "plain_text", "text": "Optional"},
                },
                "optional": True,
            },
            {
                "type": "input",
                "block_id": "message",
                "label": {"type": "plain_text", "text": "Message"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "message_input",
                    "initial_value": default_message,
                    "multiline": False,
                },
            },
        ]
    )
    return {
        "type": "modal",
        "callback_id": "reach_submit",
        "private_metadata": json.dumps(
            {"requester_id": requester_id, "target_id": target_id}, separators=(",", ":")
        ),
        "title": {"type": "plain_text", "text": "Reach someone"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def _button(
    candidate: Candidate,
    target_id: str,
    ping_id: str | None,
    names: dict[str, str] | None,
) -> dict[str, Any]:
    value = {"candidate_id": candidate.user_id, "target_id": target_id}
    if ping_id:
        value["ping_id"] = ping_id
    # Button text only supports plain_text, which Slack never resolves
    # <@Uxxxx> mention syntax inside -- it shows the literal characters.
    # Use the resolved display name when we have one, falling back to the
    # raw ID only if the lookup failed for some reason.
    label = (names or {}).get(candidate.user_id, candidate.user_id)
    return {
        "type": "button",
        "action_id": "ping_candidate",
        "text": {"type": "plain_text", "text": f"Ping {label}"},
        "accessibility_label": f"Ping {label} about reaching the target",
        "value": json.dumps(value),
    }


def render_suggestions(
    target_id: str,
    ranked: RankedCandidates,
    note: str | None = None,
    ping_ids: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Reaching <@{target_id}>"}}
    ]
    if note:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    for index, (label, candidates) in enumerate(
        (("Active now", ranked.active), ("Offline", ranked.offline))
    ):
        if index:
            blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{label}*"}})
        if candidates:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        _button(c, target_id, ping_ids.get(c.user_id) if ping_ids else None, names)
                        for c in candidates
                    ],
                }
            )
    return blocks


def render_recipient_actions(
    *,
    ping_id: str,
    reach_request_id: str,
    requester_id: str,
    target_id: str,
    candidate_id: str,
) -> dict[str, Any]:
    value = json.dumps(
        {
            "ping_id": ping_id,
            "reach_request_id": reach_request_id,
            "requester_id": requester_id,
            "target_id": target_id,
            "candidate_id": candidate_id,
        },
        separators=(",", ":"),
    )
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": action_id,
                "text": {"type": "plain_text", "text": label},
                "accessibility_label": label,
                "value": value,
            }
            for action_id, label in (
                ("outcome_helped", "\u2705 I know"),
                ("outcome_unknown", "\u274c Don't know"),
                ("outcome_more", "\U0001f4ac Reply with more"),
            )
        ],
    }


def render_ping_modal(
    candidate_id: str, target_id: str, note: str | None = None, ping_id: str = ""
) -> dict[str, Any]:
    text = note or f"Hey, trying to reach <@{target_id}> \u2014 do you know if they're around?"
    return {
        "type": "modal",
        "callback_id": "ping_submit",
        "private_metadata": f"{candidate_id}:{target_id}:{ping_id}",
        "title": {"type": "plain_text", "text": "Ping teammate"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "message",
                "label": {"type": "plain_text", "text": "Message"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "message_input",
                    "initial_value": text,
                    "multiline": False,
                },
            }
        ],
    }


def render_reply_modal(ping_id: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "reply_more_submit",
        "private_metadata": ping_id,
        "title": {"type": "plain_text", "text": "Reply to Reach"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "reply",
                "label": {"type": "plain_text", "text": "Reply"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "reply_input",
                    "multiline": False,
                },
            }
        ],
    }


def render_why(ranked: RankedCandidates) -> list[dict[str, Any]]:
    lines = []
    for candidate in (*ranked.active, *ranked.offline):
        presence = "active" if candidate.presence == "active" else "offline"
        evidence = []
        if candidate.channel_proximity:
            evidence.append("shared public channel")
        if candidate.thread_recency:
            evidence.append("recent public thread")
        details = ", ".join(evidence) or "public signal"
        lines.append(f"<@{candidate.user_id}> \u2014 {presence} \u00b7 {details}")
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Why these people"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines) or "No candidates found."},
        },
    ]
