from reach_bot.ranking import Candidate, RankedCandidates
from reach_bot.rendering import render_suggestions


def test_rendering_is_ephemeral_friendly_and_button_only():
    blocks = render_suggestions(
        "target", RankedCandidates((Candidate("a", presence="active"),), ())
    )
    assert blocks[0]["type"] == "header"
    assert any(
        e.get("action_id") == "ping_candidate" for b in blocks for e in b.get("elements", [])
    )
    assert not any(b.get("type") == "divider" and i == 0 for i, b in enumerate(blocks))
