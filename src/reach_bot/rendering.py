from __future__ import annotations

import json
from typing import Any

from reach_bot.ranking import Candidate, RankedCandidates


def _button(candidate: Candidate, target_id: str, ping_id: str | None) -> dict[str, Any]:
    value = {"candidate_id": candidate.user_id, "target_id": target_id}
    if ping_id:
        value["ping_id"] = ping_id
    return {
        "type": "button",
        "action_id": "ping_candidate",
        "text": {"type": "plain_text", "text": f"Ping <@{candidate.user_id}>"},
        "value": json.dumps(value),
    }


def render_suggestions(
    target_id: str,
    ranked: RankedCandidates,
    note: str | None = None,
    ping_ids: dict[str, str] | None = None,
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
                        _button(c, target_id, ping_ids.get(c.user_id) if ping_ids else None)
                        for c in candidates
                    ],
                }
            )
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "why_these_people",
                    "text": {"type": "plain_text", "text": "ⓘ Why these people"},
                    "value": target_id,
                }
            ],
        }
    )
    return blocks


def render_ping_modal(
    candidate_id: str, target_id: str, note: str | None = None, ping_id: str = ""
) -> dict[str, Any]:
    text = note or f"Hey, trying to reach <@{target_id}> — do you know if they're around?"
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


def render_why(ranked: RankedCandidates) -> list[dict[str, Any]]:
    lines = []
    for candidate in (*ranked.active, *ranked.offline):
        presence = "active" if candidate.presence == "active" else "offline"
        evidence = []
        if candidate.channel_proximity:
            evidence.append(f"shared-channel signal {candidate.channel_proximity:.3f}")
        if candidate.thread_recency:
            evidence.append(f"recent-thread signal {candidate.thread_recency:.3f}")
        details = ", ".join(evidence) or "public signal"
        lines.append(f"<@{candidate.user_id}> — {presence} · {details}")
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Why these people"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines) or "No candidates found."},
        },
    ]
