from __future__ import annotations

import json
from typing import Any

from reach_bot.ranking import Candidate, RankedCandidates

DEFAULT_MESSAGE = "Do you know where they are or how to reach them?"
CUTOFF_STATUS = "Someone already confirmed a location for this — thanks!"
THANK_YOU_STATUS = "Thanks for your response!"


def mrkdwn_section(text: str) -> dict[str, Any]:
    """Minimal section block; the building block for recipient-facing text."""
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def broadcast_text(message: str, requester_id: str, target_id: str) -> str:
    """Prefix the composed text with the Slack broadcast-mention `<!channel>`.

    `<!channel>` (NOT `<@channel>` — that is a literal user mention and will
    not notify anyone) is used for public-channel broadcast posts only.
    """
    if target_id:
        context = f"needs a quick word with <@{target_id}>"
    else:
        context = "is asking around"
    return f"<!channel> Yo, Reach'em here, <@{requester_id}> {context}. (s)He says:\n{message}"


def dm_text(message: str, requester_id: str, target_id: str) -> str:
    """The DM framing used for hand-picked recipients (no `<!channel>` prefix)."""
    return f"Reach, relaying for <@{requester_id}> about <@{target_id}>:\n{message}"


def render_reach_stage1() -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "reach_stage1_submit",
        "title": {"type": "plain_text", "text": "Reach someone"},
        "submit": {"type": "plain_text", "text": "Next"},
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
                },
            }
        ],
    }


def render_quick_people_modal(
    target_id: str, *, requester_id: str, target_name: str | None = None
) -> dict[str, Any]:
    """Compact `/reach @person` modal: manual recipients plus message only."""
    target_name = target_name or target_id
    return {
        "type": "modal",
        "callback_id": "reach_quick_people_submit",
        "private_metadata": json.dumps(
            {"requester_id": requester_id, "target_id": target_id, "target_name": target_name},
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": "Reach someone"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "candidates",
                "label": {"type": "plain_text", "text": "Who should we ask?"},
                "element": {
                    "type": "multi_users_select",
                    "action_id": "candidates",
                    "placeholder": {"type": "plain_text", "text": "Add people who might know"},
                },
                "optional": False,
            },
            _quick_message_block(target_name),
        ],
    }


def render_quick_channels_modal(
    *, requester_id: str, target_id: str = "", initial_channels: list[str] | None = None,
    target_name: str | None = None,
) -> dict[str, Any]:
    """Compact `/reach #channel` modal with no implicit channel assumption."""
    channels: dict[str, Any] = {
        "type": "multi_conversations_select",
        "action_id": "channels_choice",
        "placeholder": {"type": "plain_text", "text": "Choose channels"},
        "filter": {"include": ["public", "private"]},
    }
    # Only explicit command arguments are initialised. In particular, never
    # search for or assume a channel named "general".
    if initial_channels:
        channels["initial_conversations"] = initial_channels
    return {
        "type": "modal",
        "callback_id": "reach_quick_channels_submit",
        "private_metadata": json.dumps(
            {
                "requester_id": requester_id,
                "target_id": target_id,
                "target_name": target_name or target_id,
            },
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": "Reach a channel"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "broadcast_channels",
                "label": {"type": "plain_text", "text": "Channels to ask"},
                "element": channels,
                "optional": False,
            },
            _quick_message_block(target_name or target_id),
        ],
    }


def _quick_message_block(target_name: str) -> dict[str, Any]:
    initial = (
        f"Have you seen {target_name}? " + DEFAULT_MESSAGE if target_name else DEFAULT_MESSAGE
    )
    return {
        "type": "input",
        "block_id": "message",
        "label": {"type": "plain_text", "text": "Message"},
        "element": {
            "type": "plain_text_input",
            "action_id": "message_input",
            "initial_value": initial,
            "placeholder": {"type": "plain_text", "text": "Type a short message"},
            "multiline": False,
        },
    }


SCOPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("none", "None (just the people above)"),
    ("channel", "Everyone in a channel"),
    ("workspace", "Everyone in the workspace"),
)
RETENTION_UNITS: tuple[tuple[str, str], ...] = (
    ("minutes", "Minutes"),
    ("hours", "Hours"),
    ("days", "Days"),
)


def render_reach_audience(
    target_id: str,
    *,
    requester_id: str,
    target_name: str | None = None,
    scope: str = "none",
    initial_candidates: list[str] | None = None,
    initial_channel: str | None = None,
) -> dict[str, Any]:
    """Audience stage: recipients and optional broadcast scope.

    The channel picker is intentionally part of this stage. Selecting a
    channel only changes this view; it never creates a separate channel
    modal.
    """
    if scope not in {value for value, _ in SCOPE_OPTIONS}:
        scope = "none"
    if target_name is None:
        target_name = target_id
    candidates_block: dict[str, Any] = {
        "type": "input",
        "block_id": "candidates",
        "label": {"type": "plain_text", "text": "Who should we ask?"},
        "element": {
            "type": "multi_users_select",
            "action_id": "candidates",
            "placeholder": {"type": "plain_text", "text": "Add people who might know"},
        },
        "optional": True,
    }
    if initial_candidates:
        candidates_block["element"]["initial_users"] = initial_candidates

    options = [
        {"text": {"type": "plain_text", "text": label}, "value": value}
        for value, label in SCOPE_OPTIONS
    ]
    blocks: list[dict[str, Any]] = [
        candidates_block,
        {
            "type": "input",
            "block_id": "broadcast_scope",
            "label": {"type": "plain_text", "text": "Also ask"},
            "element": {
                "type": "radio_buttons",
                "action_id": "scope_choice",
                "options": options,
                "initial_option": next(o for o in options if o["value"] == scope),
            },
        },
    ]
    if scope == "channel":
        channel_element: dict[str, Any] = {
            "type": "conversations_select",
            "action_id": "channel_choice",
            "placeholder": {"type": "plain_text", "text": "Pick a channel"},
            "filter": {"include": ["public", "private"]},
        }
        if initial_channel:
            channel_element["initial_conversation"] = initial_channel
        blocks.append(
            {
                "type": "input",
                "block_id": "broadcast_channel",
                "label": {"type": "plain_text", "text": "Channel"},
                "element": channel_element,
            }
        )
    return {
        "type": "modal",
        "callback_id": "reach_audience_submit",
        "private_metadata": json.dumps(
            {"requester_id": requester_id, "target_id": target_id, "target_name": target_name},
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": "Who should we ask?"},
        "submit": {"type": "plain_text", "text": "Next"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


def render_reach_message(
    target_id: str,
    *,
    requester_id: str,
    target_name: str | None = None,
    candidates: list[str] | None = None,
    scope: str = "none",
    channel_id: str | None = None,
    channel_ids: list[str] | None = None,
    message_value: str | None = None,
    retention_amount: str | None = None,
    retention_unit: str = "hours",
) -> dict[str, Any]:
    """Message stage: compose the message and choose its response window."""
    if target_name is None:
        target_name = target_id
    if retention_unit not in {value for value, _ in RETENTION_UNITS}:
        retention_unit = "hours"
    unit_options = [
        {"text": {"type": "plain_text", "text": label}, "value": value}
        for value, label in RETENTION_UNITS
    ]
    if retention_unit not in {value for value, _ in RETENTION_UNITS}:
        retention_unit = "hours"
    blocks: list[dict[str, Any]] = [
        {
            "type": "input",
            "block_id": "message",
            "label": {"type": "plain_text", "text": "Message"},
            "element": {
                "type": "plain_text_input",
                "action_id": "message_input",
                "initial_value": message_value
                or (f"Have you seen {target_name}? " + DEFAULT_MESSAGE),
                "placeholder": {"type": "plain_text", "text": "Type a short message"},
                "multiline": False,
            },
        },
        {
            "type": "input",
            "block_id": "retention_amount",
            "label": {"type": "plain_text", "text": "Response window"},
            "optional": False,
            "element": {
                "type": "plain_text_input",
                "action_id": "retention_amount_input",
                "initial_value": retention_amount or "",
                "placeholder": {"type": "plain_text", "text": "Stop accepting replies after…"},
                "multiline": False,
            },
        },
        {
            "type": "input",
            "block_id": "retention_unit",
            "label": {"type": "plain_text", "text": "Time unit"},
            "element": {
                "type": "static_select",
                "action_id": "retention_unit_choice",
                "options": unit_options,
                "initial_option": next(
                    option for option in unit_options if option["value"] == retention_unit
                ),
            },
        },
    ]
    return {
        "type": "modal",
        "callback_id": "reach_message_submit",
        "private_metadata": json.dumps(
            {
                "requester_id": requester_id,
                "target_id": target_id,
                "target_name": target_name,
                "candidates": candidates or [],
                "scope": scope,
                "channel_id": channel_id or "",
                "channel_ids": channel_ids or ([channel_id] if channel_id else []),
            },
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": "Compose your message"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


# Kept as a small compatibility alias for callers that used the old renderer.
render_reach_stage2 = render_reach_audience


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
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Three reply actions for a recipient message.

    `candidate_id` is required for DMs (one known recipient) but omitted
    for broadcast channel posts, where the clicker's identity is read
    from `body["user"]["id"]` at click time (lazy response model).
    """
    value: dict[str, str] = {
        "ping_id": ping_id,
        "reach_request_id": reach_request_id,
        "requester_id": requester_id,
        "target_id": target_id,
    }
    if candidate_id:
        value["candidate_id"] = candidate_id
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": action_id,
                "text": {"type": "plain_text", "text": label},
                "accessibility_label": label,
                # Slack requires button `value` to be a string; handlers decode
                # it with decode_action_value (JSON with ast fallback).
                "value": json.dumps(value, separators=(",", ":")),
            }
            for action_id, label in (
                ("outcome_helped", "\u2705 I know"),
                ("outcome_unknown", "\u274c I don't know"),
                ("outcome_more", "\U0001f4ac Custom message"),
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


def render_location_modal(ping_id: str, channel_id: str = "") -> dict[str, Any]:
    """Follow-up modal for "I know": one single-line location field.

    ``channel_id`` rides through ``private_metadata`` so modal submissions
    (which carry no channel context of their own) can still send the
    per-responder ephemeral acknowledgment on broadcast messages.
    """
    return {
        "type": "modal",
        "callback_id": "location_submit",
        "private_metadata": json.dumps(
            {"ping_id": ping_id, "channel_id": channel_id}, separators=(",", ":")
        ),
        "title": {"type": "plain_text", "text": "I know"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "location",
                "label": {
                    "type": "plain_text",
                    "text": "Do you know where they are?",
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "location_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Where or how can they reach them?",
                    },
                    "multiline": False,
                },
            }
        ],
    }


def render_reply_modal(ping_id: str, channel_id: str = "") -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "reply_more_submit",
        "private_metadata": json.dumps(
            {"ping_id": ping_id, "channel_id": channel_id}, separators=(",", ":")
        ),
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


def render_reach_stage3(
    target_id: str = "",
    requester_id: str = "",
    message_value: str = "",
    initial_channels: list[str] | None = None,
    retention_amount: str | None = None,
    retention_unit: str = "hours",
) -> dict[str, Any]:
    """Stage 3: multi-channel picker shown when 'channel' scope chosen."""
    if retention_unit not in {value for value, _ in RETENTION_UNITS}:
        retention_unit = "hours"
    channels_element: dict[str, Any] = {
        "type": "multi_conversations_select",
        "action_id": "channels_choice",
        "placeholder": {"type": "plain_text", "text": "Select channels"},
        "filter": {"include": ["public"]},
        "default_to_current_conversation": False,
    }
    if initial_channels:
        channels_element["initial_conversations"] = initial_channels

    return {
        "type": "modal",
        "callback_id": "reach_stage3_submit",
        "private_metadata": json.dumps(
            {"requester_id": requester_id, "target_id": target_id},
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": "Select channels"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "broadcast_channels",
                "label": {"type": "plain_text", "text": "Channels to post in"},
                "element": channels_element,
                "optional": False,
            },
            {
                "type": "input",
                "block_id": "message",
                "label": {"type": "plain_text", "text": "Message"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "message_input",
                    "initial_value": message_value,
                    "placeholder": {"type": "plain_text", "text": "Type a short message"},
                    "multiline": False,
                },
            },
            {
                "type": "input",
                "block_id": "retention_amount",
                "label": {"type": "plain_text", "text": "Keep the message for"},
                "optional": False,
                "element": {
                    "type": "plain_text_input",
                    "action_id": "retention_amount_input",
                    "initial_value": retention_amount or "",
                    "placeholder": {"type": "plain_text", "text": "e.g. 2"},
                    "multiline": False,
                },
            },
            {
                "type": "input",
                "block_id": "retention_unit",
                "label": {"type": "plain_text", "text": "Time unit"},
                "element": {
                    "type": "static_select",
                    "action_id": "retention_unit_choice",
                    "options": [
                        {"text": {"type": "plain_text", "text": label}, "value": value}
                        for value, label in RETENTION_UNITS
                    ],
                    "initial_option": {
                        "text": {"type": "plain_text", "text": next(
                            label for value, label in RETENTION_UNITS if value == retention_unit
                        )},
                        "value": retention_unit,
                    },
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "Your message will be posted to each selected public channel "
                        "with `<!channel>`."
                    ),
                },
            },
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
