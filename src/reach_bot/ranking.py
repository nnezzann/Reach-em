from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol


@dataclass(frozen=True)
class Affinity:
    score: float
    sample_size: int


@dataclass(frozen=True)
class Candidate:
    user_id: str
    channel_proximity: float = 0.0
    thread_recency: float = 0.0
    affinity: Affinity | None = None
    presence: str = "offline"
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def affinity_boost(self) -> float:
        return self.affinity.score if self.affinity else 0.0


@dataclass(frozen=True)
class RankedCandidates:
    active: tuple[Candidate, ...]
    offline: tuple[Candidate, ...]


class SignalProvider(Protocol):
    def public_channels(self, target_id: str) -> list[str]: ...
    def channel_members(self, channel_id: str) -> list[str]: ...
    def thread_cooccurrences(
        self, target_id: str, channel_ids: list[str], since: datetime
    ) -> dict[str, float]: ...
    def presence(self, user_id: str) -> str: ...
    def affinities(self, target_id: str) -> dict[str, Affinity]: ...


class PresenceCache(Protocol):
    def get(self, user_id: str) -> str | None: ...
    def set(self, user_id: str, presence: str, ttl_seconds: int) -> None: ...


def rank_candidates(
    target_id: str,
    requester_id: str,
    provider: SignalProvider,
    *,
    max_per_bucket: int = 3,
    min_sample_threshold: int = 3,
    thread_recency_days: int = 7,
    include_threads: bool = True,
) -> RankedCandidates:
    """Generate and rank public-channel candidates without side effects."""
    channels = provider.public_channels(target_id)
    proximity: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    for channel in channels:
        members = provider.channel_members(channel)
        size = max(len(members), 1)
        weight = 1.0 / size
        for user_id in members:
            if user_id in {target_id, requester_id}:
                continue
            if weight > proximity.get(user_id, 0.0):
                proximity[user_id] = weight
            evidence.setdefault(user_id, []).append(channel)
    recency = (
        provider.thread_cooccurrences(
            target_id, channels, datetime.now(UTC) - timedelta(days=thread_recency_days)
        )
        if include_threads
        else {}
    )
    affinities = provider.affinities(target_id)
    candidates: list[Candidate] = []
    for user_id in set(proximity) | set(recency):
        affinity = affinities.get(user_id)
        eligible_affinity = (
            affinity if affinity and affinity.sample_size >= min_sample_threshold else None
        )
        candidates.append(
            Candidate(
                user_id,
                proximity.get(user_id, 0.0),
                recency.get(user_id, 0.0),
                eligible_affinity,
                provider.presence(user_id),
                tuple(evidence.get(user_id, [])),
            )
        )

    def key(candidate: Candidate) -> tuple[float, float, float, str]:
        return (
            candidate.affinity_boost,
            candidate.channel_proximity,
            candidate.thread_recency,
            candidate.user_id,
        )

    active = tuple(
        sorted((c for c in candidates if c.presence == "active"), key=key, reverse=True)[
            :max_per_bucket
        ]
    )
    offline = tuple(
        sorted((c for c in candidates if c.presence != "active"), key=key, reverse=True)[
            :max_per_bucket
        ]
    )
    return RankedCandidates(active, offline)


class CachedSignalProvider:
    def __init__(self, provider: SignalProvider, cache: PresenceCache, ttl_seconds: int) -> None:
        self.provider = provider
        self.cache = cache
        self.ttl_seconds = ttl_seconds

    def public_channels(self, target_id: str) -> list[str]:
        return self.provider.public_channels(target_id)

    def channel_members(self, channel_id: str) -> list[str]:
        return self.provider.channel_members(channel_id)

    def thread_cooccurrences(
        self, target_id: str, channel_ids: list[str], since: datetime
    ) -> dict[str, float]:
        return self.provider.thread_cooccurrences(target_id, channel_ids, since)

    def presence(self, user_id: str) -> str:
        cached = self.cache.get(user_id)
        if cached is not None:
            return cached
        presence = self.provider.presence(user_id)
        self.cache.set(user_id, presence, self.ttl_seconds)
        return presence

    def affinities(self, target_id: str) -> dict[str, Affinity]:
        return self.provider.affinities(target_id)
