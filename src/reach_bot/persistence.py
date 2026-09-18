from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import uuid4

from reach_bot.ranking import Affinity


@dataclass(frozen=True)
class ReachRequest:
    id: str
    requester_id: str
    target_id: str
    created_at: datetime


@dataclass(frozen=True)
class Ping:
    id: str
    requester_id: str
    target_id: str
    candidate_id: str
    channel_context: str | None
    presence_at_ping: str
    created_at: datetime
    reach_request_id: str = ""


@dataclass(frozen=True)
class PingOutcome:
    ping_id: str
    outcome: str
    responded_at: datetime | None = None
    response_latency_seconds: int | None = None


class ReachRepository(Protocol):
    def create_reach_request(self, requester_id: str, target_id: str) -> ReachRequest: ...

    def create_ping(
        self,
        reach_request_id: str,
        requester_id: str,
        target_id: str,
        candidate_id: str,
        channel_context: str | None,
        presence: str,
    ) -> Ping: ...
    def record_outcome(self, outcome: PingOutcome) -> None: ...
    def get_ping(self, ping_id: str) -> Ping | None: ...
    def affinities(self, target_id: str) -> dict[str, Affinity]: ...


class PresenceCache(Protocol):
    def get(self, user_id: str) -> str | None: ...
    def set(self, user_id: str, presence: str, ttl_seconds: int) -> None: ...


class MemoryRepository:
    def __init__(self) -> None:
        self.reach_requests: list[ReachRequest] = []
        self.pings: list[Ping] = []
        self.outcomes: dict[str, PingOutcome] = {}

    def create_reach_request(self, requester_id: str, target_id: str) -> ReachRequest:
        request = ReachRequest(str(uuid4()), requester_id, target_id, datetime.now(UTC))
        self.reach_requests.append(request)
        return request

    def create_ping(
        self,
        reach_request_id: str,
        requester_id: str,
        target_id: str,
        candidate_id: str,
        channel_context: str | None,
        presence: str,
    ) -> Ping:
        ping = Ping(
            str(uuid4()),
            requester_id,
            target_id,
            candidate_id,
            channel_context,
            presence,
            datetime.now(UTC),
            reach_request_id,
        )
        self.pings.append(ping)
        return ping

    def record_outcome(self, outcome: PingOutcome) -> None:
        self.outcomes[outcome.ping_id] = outcome

    def get_ping(self, ping_id: str) -> Ping | None:
        return next((ping for ping in self.pings if ping.id == ping_id), None)

    def affinities(self, target_id: str) -> dict[str, Affinity]:
        return {}


class RedisPresenceCache:
    """Small adapter; Redis is deliberately limited to short-lived presence."""

    def __init__(self, client: object) -> None:
        self.client = client

    def get(self, user_id: str) -> str | None:
        value = self.client.get(f"presence:{user_id}")  # type: ignore[attr-defined]
        if isinstance(value, bytes):
            return value.decode()
        return cast(str | None, value)

    def set(self, user_id: str, presence: str, ttl_seconds: int) -> None:
        self.client.setex(f"presence:{user_id}", ttl_seconds, presence)  # type: ignore[attr-defined]


class PostgresRepository:
    """Synchronous repository for short Slack interaction transactions."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def create_ping(
        self,
        reach_request_id: str,
        requester_id: str,
        target_id: str,
        candidate_id: str,
        channel_context: str | None,
        presence: str,
    ) -> Ping:
        ping = Ping(
            str(uuid4()),
            requester_id,
            target_id,
            candidate_id,
            channel_context,
            presence,
            datetime.now(UTC),
            reach_request_id,
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pings
                  (id, reach_request_id, requester_id, target_id, candidate_id, channel_context,
                   presence_at_ping, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    ping.id,
                    ping.reach_request_id,
                    ping.requester_id,
                    ping.target_id,
                    ping.candidate_id,
                    ping.channel_context,
                    ping.presence_at_ping,
                    ping.created_at,
                ),
            )
        self.connection.commit()
        return ping

    def create_reach_request(self, requester_id: str, target_id: str) -> ReachRequest:
        request = ReachRequest(str(uuid4()), requester_id, target_id, datetime.now(UTC))
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO reach_requests (id, requester_id, target_id, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (request.id, request.requester_id, request.target_id, request.created_at),
            )
        self.connection.commit()
        return request

    def record_outcome(self, outcome: PingOutcome) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ping_outcomes
                  (id, ping_id, outcome, responded_at, response_latency_seconds)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (ping_id) DO UPDATE SET
                  outcome = EXCLUDED.outcome,
                  responded_at = EXCLUDED.responded_at,
                  response_latency_seconds = EXCLUDED.response_latency_seconds
                """,
                (
                    str(uuid4()),
                    outcome.ping_id,
                    outcome.outcome,
                    outcome.responded_at,
                    outcome.response_latency_seconds,
                ),
            )
        self.connection.commit()

    def get_ping(self, ping_id: str) -> Ping | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, requester_id, target_id, candidate_id, channel_context,
                       presence_at_ping, created_at, reach_request_id
                FROM pings WHERE id = %s
                """,
                (ping_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return Ping(*row)

    def affinities(self, target_id: str) -> dict[str, Affinity]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT candidate_id, score, sample_size FROM affinity_scores WHERE target_id = %s",
                (target_id,),
            )
            return {
                str(candidate_id): Affinity(float(score), int(sample_size))
                for candidate_id, score, sample_size in cursor.fetchall()
            }
