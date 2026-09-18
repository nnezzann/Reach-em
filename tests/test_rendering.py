from reach_bot.ranking import Candidate, RankedCandidates
from reach_bot.rendering import render_reach_stage1, render_reach_stage2, render_suggestions


def test_rendering_is_ephemeral_friendly_and_button_only():
    blocks = render_suggestions(
        "target", RankedCandidates((Candidate("a", presence="active"),), ())
    )
    assert blocks[0]["type"] == "header"
    assert any(
        e.get("action_id") == "ping_candidate" for b in blocks for e in b.get("elements", [])
    )
    assert not any(b.get("type") == "divider" and i == 0 for i, b in enumerate(blocks))


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
