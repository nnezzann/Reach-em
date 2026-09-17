from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import exp

from reach_bot.persistence import Ping, PingOutcome


@dataclass(frozen=True)
class AffinityAggregate:
    target_id: str
    candidate_id: str
    score: float
    sample_size: int


OUTCOME_VALUES = {
    "helped": 1.0,
    "relayed": 0.75,
    "unknown": 0.0,
    "no_response": 0.0,
    "wrong_person": -0.5,
}


def aggregate_affinity(
    pings: list[Ping],
    outcomes: list[PingOutcome],
    *,
    half_life_days: float = 30.0,
    now: datetime | None = None,
) -> list[AffinityAggregate]:
    """Calculate a decayed outcome average without exposing individual rankings."""
    reference = now or datetime.now(UTC)
    by_ping = {ping.id: ping for ping in pings}
    grouped: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for outcome in outcomes:
        ping = by_ping.get(outcome.ping_id)
        value = OUTCOME_VALUES.get(outcome.outcome)
        if ping is None or value is None:
            continue
        age_days = max((reference - ping.created_at).total_seconds() / 86400, 0.0)
        weight = exp(-age_days * 0.69314718056 / half_life_days)
        grouped.setdefault((ping.target_id, ping.candidate_id), []).append((value, weight))
    return [
        AffinityAggregate(
            target_id,
            candidate_id,
            sum(value * weight for value, weight in values) / sum(weight for _, weight in values),
            len(values),
        )
        for (target_id, candidate_id), values in grouped.items()
    ]
