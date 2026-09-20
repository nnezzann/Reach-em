from reach_bot.ranking import Candidate, RankedCandidates
from reach_bot.rendering import (
    mrkdwn_section,
    render_location_modal,
    render_reach_stage1,
    render_reach_stage2,
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


def test_reach_stage2_groups_candidates_and_prechecks_top_two():
    ranked = RankedCandidates(
        (
            Candidate("active-1", presence="active"),
            Candidate("active-2", presence="active"),
        ),
        (Candidate("offline-1"),),
    )

    view = render_reach_stage2("target", ranked, requester_id="requester")

    assert view["callback_id"] == "reach_submit"
    checkbox_blocks = [
        block for block in view["blocks"] if block["element"]["type"] == "checkboxes"
    ]
    assert [block["label"]["text"] for block in checkbox_blocks] == ["Active now", "Offline"]
    assert [option["value"] for option in checkbox_blocks[0]["element"]["initial_options"]] == [
        "active-1",
        "active-2",
    ]
    assert any(block["element"]["type"] == "multi_users_select" for block in view["blocks"])


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
    assert view["private_metadata"] == "ping-1"
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
