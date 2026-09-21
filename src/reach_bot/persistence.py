from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from reach_bot.ranking import Affinity

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReachRequest:
    id: str
    requester_id: str
    target_id: str
    created_at: datetime
    known_count: int = 0


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
    channel: str = ""
    message_ts: str = ""
    # Broadcast-only closure counters (Pool 2). Broadcast pings are exactly
    # those with candidate_id == "" (one ping per posted channel); DM pings
    # (Pool 1) never touch these — they close via the hunt-wide global
    # known_count on reach_requests instead. The two pools are independent:
    # no shared counter, no cross-influence in either direction.
    local_know_count: int = 0
    local_total_count: int = 0


@dataclass(frozen=True)
class PingOutcome:
    ping_id: str
    outcome: str
    responded_at: datetime | None = None
    response_latency_seconds: int | None = None
    location: str | None = None


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
    def get_outcome(self, ping_id: str) -> PingOutcome | None: ...

    def set_ping_delivery(self, ping_id: str, channel: str, message_ts: str) -> None:
        """Store where a sent recipient message lives so it can be edited later."""
        ...

    def increment_known_count(self, reach_request_id: str) -> int:
        """Atomically count one more "I know" response and return the new count.

        Pool 1 (manual/hand-picked DM recipients) only — broadcast responses
        must never touch this counter.
        """
        ...

    def increment_broadcast_counts(
        self, ping_id: str, counts_toward_known: bool
    ) -> tuple[int, int]:
        """Atomically bump a broadcast message's local counters.

        Pool 2 (broadcast channel/workspace posts) only — entirely separate
        from the DM-side global counter. ``local_total_count`` increments on
        every response type; ``local_know_count`` only when
        ``counts_toward_known`` is True. Returns the post-increment
        ``(local_know_count, local_total_count)`` pair in one atomic
        statement so two near-simultaneous responses on the same broadcast
        message cannot both read a stale count.
        """
        ...

    def get_unresponded_pings(self, reach_request_id: str) -> list[Ping]:
        """Delivered DM pings for a request that have not produced an outcome yet.

        Deliberately excludes broadcast pings: the DM global-threshold sweep
        may only ever close manual/hand-picked DM messages. Broadcast
        messages are closed exclusively by their own local thresholds.
        """
        ...

    def known_count(self, reach_request_id: str) -> int:
        """Current "I know" count for a request (read only)."""
        ...

    def affinities(self, target_id: str) -> dict[str, Affinity]: ...


class PresenceCache(Protocol):
    def get(self, user_id: str) -> str | None: ...
    def set(self, user_id: str, presence: str, ttl_seconds: int) -> None: ...


class MemoryRepository:
    def __init__(self) -> None:
        self.reach_requests: list[ReachRequest] = []
        self.pings: list[Ping] = []
        self.outcomes: dict[str, PingOutcome] = {}
        self._known_counts: dict[str, int] = {}
        self._local_counts: dict[str, tuple[int, int]] = {}
        self._count_lock = threading.Lock()

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

    def get_outcome(self, ping_id: str) -> PingOutcome | None:
        return self.outcomes.get(ping_id)

    def set_ping_delivery(self, ping_id: str, channel: str, message_ts: str) -> None:
        self.pings = [
            replace(ping, channel=channel, message_ts=message_ts)
            if ping.id == ping_id
            else ping
            for ping in self.pings
        ]

    def increment_known_count(self, reach_request_id: str) -> int:
        with self._count_lock:
            count = self._known_counts.get(reach_request_id, 0) + 1
            self._known_counts[reach_request_id] = count
        return count

    def known_count(self, reach_request_id: str) -> int:
        return self._known_counts.get(reach_request_id, 0)

    def increment_broadcast_counts(
        self, ping_id: str, counts_toward_known: bool
    ) -> tuple[int, int]:
        with self._count_lock:
            know, total = self._local_counts.get(ping_id, (0, 0))
            know = know + 1 if counts_toward_known else know
            total += 1
            self._local_counts[ping_id] = (know, total)
        return know, total

    def get_unresponded_pings(self, reach_request_id: str) -> list[Ping]:
        # candidate_id == "" marks a broadcast ping; the DM-side global
        # sweep must never close those (they own their local thresholds).
        return [
            ping
            for ping in self.pings
            if ping.reach_request_id == reach_request_id
            and ping.message_ts
            and ping.candidate_id != ""
            and ping.id not in self.outcomes
        ]

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


def apply_migrations(connection: Any) -> None:
    """Run every ``.sql`` migration in ``src/reach_bot/migrations`` in order.

    Called once when ``PostgresRepository`` is instantiated, before any
    query that assumes the tables exist. The migrations are idempotent
    (``CREATE TABLE IF NOT EXISTS``), so re-running on every startup is safe.
    """
    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.is_dir():
        return
    for migration_file in sorted(migrations_dir.glob("*.sql")):
        sql = migration_file.read_text()
        with connection.cursor() as cursor:
            cursor.execute(sql)
        connection.commit()
        log.info("Applied migration %s", migration_file.name)


class PostgresRepository:
    """Synchronous repository for short Slack interaction transactions."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        apply_migrations(connection)

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
                  (id, ping_id, outcome, responded_at, response_latency_seconds, location)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (ping_id) DO UPDATE SET
                  outcome = EXCLUDED.outcome,
                  responded_at = EXCLUDED.responded_at,
                  response_latency_seconds = EXCLUDED.response_latency_seconds,
                  location = EXCLUDED.location
                """,
                (
                    str(uuid4()),
                    outcome.ping_id,
                    outcome.outcome,
                    outcome.responded_at,
                    outcome.response_latency_seconds,
                    outcome.location,
                ),
            )
        self.connection.commit()

    def get_ping(self, ping_id: str) -> Ping | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, requester_id, target_id, candidate_id, channel_context,
                       presence_at_ping, created_at, reach_request_id, channel, message_ts,
                       local_know_count, local_total_count
                FROM pings WHERE id = %s
                """,
                (ping_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return Ping(*row)

    def get_outcome(self, ping_id: str) -> PingOutcome | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT outcome, responded_at, response_latency_seconds, location
                FROM ping_outcomes WHERE ping_id = %s
                """,
                (ping_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return PingOutcome(ping_id, *row)

    def set_ping_delivery(self, ping_id: str, channel: str, message_ts: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pings SET channel = %s, message_ts = %s WHERE id = %s
                """,
                (channel, message_ts, ping_id),
            )
        self.connection.commit()

    def increment_known_count(self, reach_request_id: str) -> int:
        """Single-statement increment-and-check; safe against races."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE reach_requests SET known_count = known_count + 1
                WHERE id = %s
                RETURNING known_count
                """,
                (reach_request_id,),
            )
            row = cursor.fetchone()
        self.connection.commit()
        return int(row[0]) if row is not None else 0

    def known_count(self, reach_request_id: str) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT known_count FROM reach_requests WHERE id = %s",
                (reach_request_id,),
            )
            row = cursor.fetchone()
        return int(row[0]) if row is not None else 0

    def increment_broadcast_counts(
        self, ping_id: str, counts_toward_known: bool
    ) -> tuple[int, int]:
        """Single-statement atomic increment-and-check for one broadcast message."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pings
                SET local_know_count = local_know_count + %s,
                    local_total_count = local_total_count + 1
                WHERE id = %s
                RETURNING local_know_count, local_total_count
                """,
                (1 if counts_toward_known else 0, ping_id),
            )
            row = cursor.fetchone()
        self.connection.commit()
        if row is None:
            return 0, 0
        return int(row[0]), int(row[1])

    def get_unresponded_pings(self, reach_request_id: str) -> list[Ping]:
        # candidate_id <> '' excludes broadcast pings: this lookup feeds the
        # DM-side global-threshold sweep, which must only ever close
        # manual/hand-picked DM messages. Broadcast messages are closed
        # exclusively by their own local thresholds.
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, requester_id, target_id, candidate_id, channel_context,
                       presence_at_ping, created_at, reach_request_id, channel, message_ts,
                       local_know_count, local_total_count
                FROM pings
                WHERE reach_request_id = %s
                  AND message_ts <> ''
                  AND candidate_id <> ''
                  AND NOT EXISTS (
                      SELECT 1 FROM ping_outcomes WHERE ping_id = pings.id
                  )
                """,
                (reach_request_id,),
            )
            rows = cursor.fetchall()
        return [Ping(*row) for row in rows]

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
