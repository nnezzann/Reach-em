from datetime import UTC, datetime, timedelta

from reach_bot.affinity import aggregate_affinity
from reach_bot.persistence import Ping, PingOutcome


def test_aggregate_affinity_decays_recent_outcomes_more_than_old_ones() -> None:
    now = datetime(2026, 1, 31, tzinfo=UTC)
    pings = [
        Ping("old", "requester", "target", "candidate", None, "offline", now - timedelta(days=30)),
        Ping("new", "requester", "target", "candidate", None, "active", now),
    ]
    outcomes = [PingOutcome("old", "wrong_person"), PingOutcome("new", "helped")]

    result = aggregate_affinity(pings, outcomes, half_life_days=30, now=now)

    assert result[0].sample_size == 2
    assert 0 < result[0].score < 1
