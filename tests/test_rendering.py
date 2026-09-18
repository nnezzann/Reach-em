from reach_bot.ranking import Candidate, RankedCandidates
from reach_bot.rendering import (
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


def test_reach_stage1_has_dispatching_target_picker():
    view = render_reach_stage1()

    element = view["blocks"][0]["element"]
    assert element["type"] == "users_select"
    assert element["dispatch_action"] is True


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
