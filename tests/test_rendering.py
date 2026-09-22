import json

from reach_bot.ranking import Candidate, RankedCandidates
from reach_bot.rendering import (
    mrkdwn_section,
    render_location_modal,
    render_reach_stage1,
    render_reach_stage2,
    render_reach_stage3,
    render_recipient_actions,
    render_suggestions,
    render_why,
)


def test_rendering_has_presence_sections_and_ping_buttons():
    blocks = render_suggestions(
        "target", RankedCandidates((Candidate("a", presence="active"),), ())
    )
    assert blocks[0]["type"] == "header"
    assert any(
        e.get("action_id") == "ping_candidate" for b in blocks for e in b.get("elements", [])
    )
    assert any(b.get("text", {}).get("text") == "*Active now*" for b in blocks)
    assert not any(
        e.get("action_id") == "why_these_people" for b in blocks for e in b.get("elements", [])
    )


def test_reach_stage1_has_submit_button_target_picker():
    view = render_reach_stage1()

    assert view["callback_id"] == "reach_stage1_submit"
    assert len(view["blocks"]) == 1
    assert view["blocks"][0]["label"]["text"] == "Who are you trying to reach?"
    element = view["blocks"][0]["element"]
    assert element["type"] == "users_select"
    assert element["action_id"] == "target_user"
    assert view["submit"]["text"] == "Next"


def test_reach_stage2_is_the_picker_modal():
    view = render_reach_stage2("target", requester_id="requester", target_name="Grace")

    assert view["callback_id"] == "reach_submit"
    assert '"target_id":"target"' in view["private_metadata"]
    assert [block["block_id"] for block in view["blocks"]] == [
        "candidates",
        "broadcast_scope",
        "message",
        "retention_amount",
        "retention_unit",
    ]
    candidates = view["blocks"][0]
    assert candidates["element"]["type"] == "multi_users_select"
    assert candidates["element"]["placeholder"]["text"] == "Add people who might know"
    assert candidates.get("optional") is True
    scope = view["blocks"][1]
    assert scope["element"]["type"] == "radio_buttons"
    assert [option["value"] for option in scope["element"]["options"]] == [
        "none",
        "channel",
        "workspace",
    ]
    assert scope["element"]["initial_option"]["value"] == "none"
    message = view["blocks"][2]
    assert message["element"]["initial_value"].startswith("Have you seen Grace?")
    assert view["blocks"][3]["element"]["action_id"] == "retention_amount_input"
    assert view["blocks"][3]["label"]["text"] == "Keep the message for"
    assert view["blocks"][3]["optional"] is False
    assert view["blocks"][4]["element"]["initial_option"]["value"] == "hours"


def test_reach_stage2_channel_picker_only_when_scope_is_channel():
    with_picker = render_reach_stage2(
        "t", requester_id="r", scope="channel", initial_channel="C1"
    )
    without_picker = render_reach_stage2("t", requester_id="r")

    assert "broadcast_channel" in [block["block_id"] for block in with_picker["blocks"]]
    channel = next(
        block for block in with_picker["blocks"] if block["block_id"] == "broadcast_channel"
    )
    assert channel["element"]["type"] == "conversations_select"
    assert channel["element"]["filter"]["include"] == ["public", "private"]
    assert channel["element"]["initial_conversation"] == "C1"
    assert "broadcast_channel" not in [block["block_id"] for block in without_picker["blocks"]]


def test_reach_stage3_requires_retention_duration():
    view = render_reach_stage3("target", "requester")

    retention = next(block for block in view["blocks"] if block["block_id"] == "retention_amount")
    assert retention["label"]["text"] == "Keep the message for"
    assert retention["optional"] is False


def test_reach_stage2_rerender_preserves_current_input():
    view = render_reach_stage2(
        "t",
        requester_id="r",
        target_name="Grace",
        scope="channel",
        initial_candidates=["U-a"],
        initial_channel="C1",
        message_value="Edited",
    )

    blocks = {block["block_id"]: block for block in view["blocks"]}
    assert blocks["candidates"]["element"]["initial_users"] == ["U-a"]
    assert blocks["broadcast_scope"]["element"]["initial_option"]["value"] == "channel"
    assert blocks["broadcast_channel"]["element"]["initial_conversation"] == "C1"
    assert blocks["message"]["element"]["initial_value"] == "Edited"


def test_recipient_actions_include_all_ping_context_and_expected_interactions():
    import json

    block = render_recipient_actions(
        ping_id="ping",
        reach_request_id="request",
        requester_id="requester",
        target_id="target",
        candidate_id="candidate",
    )

    assert [element["action_id"] for element in block["elements"]] == [
        "outcome_helped",
        "outcome_unknown",
        "outcome_more",
    ]
    assert json.loads(block["elements"][0]["value"]) == {
        "ping_id": "ping",
        "reach_request_id": "request",
        "requester_id": "requester",
        "target_id": "target",
        "candidate_id": "candidate",
    }
    assert all(element["accessibility_label"] for element in block["elements"])


def test_recipient_buttons_use_updated_labels():
    block = render_recipient_actions(
        ping_id="p", reach_request_id="r", requester_id="q", target_id="t", candidate_id="c"
    )

    assert [element["text"]["text"] for element in block["elements"]] == [
        "\u2705 I know",
        "\u274c I don't know",
        "\U0001f4ac Custom message",
    ]
    assert len(block["elements"]) == 3


def test_location_modal_is_a_single_line_input():
    view = render_location_modal("ping-1")

    assert view["callback_id"] == "location_submit"
    # channel_id threads through private_metadata for broadcast ephemeral acks.
    assert json.loads(view["private_metadata"]) == {"ping_id": "ping-1", "channel_id": ""}
    block = view["blocks"][0]
    assert block["block_id"] == "location"
    element = block["element"]
    assert element["type"] == "plain_text_input"
    assert element["action_id"] == "location_input"
    assert element["multiline"] is False
    assert element["placeholder"]["text"] == "Where or how can they reach them?"


def test_mrkdwn_section_helper_builds_minimal_section():
    assert mrkdwn_section("hello") == {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "hello"},
    }


def test_why_detail_does_not_expose_internal_signal_scores():
    blocks = render_why(
        RankedCandidates(
            (Candidate("active", presence="active", channel_proximity=0.5),),
            (),
        )
    )

    text = blocks[1]["text"]["text"]
    assert "0.5" not in text
    assert "shared public channel" in text
