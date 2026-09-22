from __future__ import annotations

import logging
import threading
from collections.abc import Callable
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
    broadcast_closed: bool = False
    expires_at: datetime | None = None
    deleted_at: datetime | None = None


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

    def set_ping_expiry(self, ping_id: str, expires_at: datetime) -> None: ...
    def get_expired_pings(self, now: datetime) -> list[Ping]: ...
    def mark_ping_deleted(self, ping_id: str, deleted_at: datetime) -> None: ...
    def get_pings_before(self, cutoff: datetime) -> list[Ping]: ...

    def increment_known_count(self, reach_request_id: str) -> int:
        """Atomically count one more "I know" response and return the new count.

        Pool 1 (manual/hand-picked DM recipients) only — broadcast responses
        must never touch this counter.
        """
        ...

    def increment_broadcast_counts(
        self, ping_id: str, counts_toward_known: bool
    ) -> tuple[int, int, bool]:
        """Atomically bump a broadcast message's local counters.

        Pool 2 (broadcast channel/workspace posts) only — entirely separate
        from the DM-side global counter. ``local_total_count`` increments on
        every response type; ``local_know_count`` only when
        ``counts_toward_known`` is True. Returns the post-increment
        ``(local_know_count, local_total_count)`` pair in one atomic
        statement so two near-simultaneous responses on the same broadcast
        message cannot both read a stale count. The bool is true only for
        the response that atomically closes the message.
        """
        ...

    def record_broadcast_response(
        self, ping_id: str, responder_id: str, outcome: PingOutcome
    ) -> bool: ...

    def broadcast_is_closed(self, ping_id: str) -> bool: ...

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
        self._broadcast_responses: set[tuple[str, str]] = set()
        self._broadcast_closed: set[str] = set()
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

    def set_ping_expiry(self, ping_id: str, expires_at: datetime) -> None:
        self.pings = [
            replace(ping, expires_at=expires_at) if ping.id == ping_id else ping
            for ping in self.pings
        ]

    def get_expired_pings(self, now: datetime) -> list[Ping]:
        return [
            ping for ping in self.pings
            if (
                ping.message_ts
                and ping.expires_at
                and ping.expires_at <= now
                and not ping.deleted_at
            )
        ]

    def mark_ping_deleted(self, ping_id: str, deleted_at: datetime) -> None:
        self.pings = [
            replace(ping, deleted_at=deleted_at) if ping.id == ping_id else ping
            for ping in self.pings
        ]

    def get_pings_before(self, cutoff: datetime) -> list[Ping]:
        return [
            ping for ping in self.pings
            if ping.message_ts and ping.created_at < cutoff and not ping.deleted_at
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
    ) -> tuple[int, int, bool]:
        with self._count_lock:
            know, total = self._local_counts.get(ping_id, (0, 0))
            if ping_id in self._broadcast_closed:
                return know, total, False
            know = know + 1 if counts_toward_known else know
            total += 1
            self._local_counts[ping_id] = (know, total)
            closed = know >= 3 or total >= 5
            if closed:
                self._broadcast_closed.add(ping_id)
        return know, total, closed

    def record_broadcast_response(
        self, ping_id: str, responder_id: str, outcome: PingOutcome
    ) -> bool:
        with self._count_lock:
            key = (ping_id, responder_id)
            if key in self._broadcast_responses or ping_id in self._broadcast_closed:
                return False
            self._broadcast_responses.add(key)
        return True

    def broadcast_is_closed(self, ping_id: str) -> bool:
        with self._count_lock:
            return ping_id in self._broadcast_closed

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
            and not ping.deleted_at
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
    query that assumes the tables exist. Applied migrations are recorded so
    restart/deploy cycles do not repeatedly take DDL locks on already-current
    tables. The advisory lock also prevents two app instances from migrating
    the database concurrently during a rolling deploy.
    """
    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.is_dir():
        return

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(hashtext('reach-em:migrations'))")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reach_schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    connection.commit()

    try:
        for migration_file in sorted(migrations_dir.glob("*.sql")):
            version = migration_file.name
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM reach_schema_migrations WHERE version = %s",
                    (version,),
                )
                already_applied = cursor.fetchone() is not None
            if already_applied:
                log.info("Migration %s already applied", version)
                continue

            if _migration_schema_is_current(connection, version):
                _record_migration(connection, version)
                log.info("Migration %s already satisfied", version)
                continue

            sql = migration_file.read_text()
            with connection.cursor() as cursor:
                cursor.execute(sql)
            _record_migration(connection, version)
            log.info("Applied migration %s", version)
    except Exception:
        connection.rollback()
        raise
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(hashtext('reach-em:migrations'))")
        connection.commit()


def _record_migration(connection: Any, version: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO reach_schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
            (version,),
        )
    connection.commit()


def _migration_schema_is_current(connection: Any, version: str) -> bool:
    """Avoid re-taking DDL locks when an older database is already upgraded."""
    requirements: dict[str, tuple[tuple[str, str], ...]] = {
        "001_initial.sql": (
            ("table", "reach_requests"),
            ("table", "pings"),
            ("table", "ping_outcomes"),
            ("table", "affinity_scores"),
        ),
        "002_response_limit.sql": (
            ("reach_requests", "known_count"),
            ("pings", "channel"),
            ("pings", "message_ts"),
            ("ping_outcomes", "location"),
        ),
        "003_broadcast_local_thresholds.sql": (
            ("pings", "local_know_count"),
            ("pings", "local_total_count"),
            ("pings", "broadcast_closed"),
            ("table", "broadcast_responses"),
        ),
        "004_message_retention.sql": (
            ("pings", "expires_at"),
            ("pings", "deleted_at"),
        ),
        # 005 is a data repair, not a schema migration, so it must execute.
        "005_repair_broadcast_threshold_state.sql": (),
    }
    required = requirements.get(version)
    if required is None or not required:
        return False

    with connection.cursor() as cursor:
        for table, column in required:
            if table == "table":
                cursor.execute(
                    """
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = current_schema() AND table_name = %s
                    """,
                    (column,),
                )
            else:
                cursor.execute(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = %s AND column_name = %s
                    """,
                    (table, column),
                )
            if cursor.fetchone() is None:
                return False
    return True


class PostgresRepository:
    """Synchronous repository for short Slack interaction transactions."""

    def __init__(
        self,
        connection: Any,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._connection = connection
        self._connection_factory = connection_factory
        self._reconnect_lock = threading.Lock()
        apply_migrations(connection)

    @property
    def connection(self) -> Any:
        """Return a usable connection, reconnecting after an idle disconnect.

        Render/Supabase can close an idle database connection while the
        process remains alive. Psycopg marks that connection as closed, but
        the repository is shared by the Socket Mode handlers and background
        cleanup task. Reconnect lazily at the next operation so one dropped
        connection does not permanently disable persistence for the process.
        """
        connection = self._connection
        if not getattr(connection, "closed", False) or self._connection_factory is None:
            return connection

        with self._reconnect_lock:
            connection = self._connection
            if not getattr(connection, "closed", False):
                return connection

            log.warning("database connection is closed; reconnecting")
            replacement = self._connection_factory()
            apply_migrations(replacement)
            self._connection = replacement
            log.info("database connection restored")
            return replacement

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
                       local_know_count, local_total_count, broadcast_closed,
                       expires_at, deleted_at
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

    def set_ping_expiry(self, ping_id: str, expires_at: datetime) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("UPDATE pings SET expires_at = %s WHERE id = %s", (expires_at, ping_id))
        self.connection.commit()

    def get_expired_pings(self, now: datetime) -> list[Ping]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, requester_id, target_id, candidate_id, channel_context,
                       presence_at_ping, created_at, reach_request_id, channel, message_ts,
                       local_know_count, local_total_count, broadcast_closed,
                       expires_at, deleted_at
                FROM pings
                WHERE message_ts <> '' AND expires_at IS NOT NULL
                  AND expires_at <= %s AND deleted_at IS NULL
                """,
                (now,),
            )
            return [Ping(*row) for row in cursor.fetchall()]

    def mark_ping_deleted(self, ping_id: str, deleted_at: datetime) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("UPDATE pings SET deleted_at = %s WHERE id = %s", (deleted_at, ping_id))
        self.connection.commit()

    def get_pings_before(self, cutoff: datetime) -> list[Ping]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, requester_id, target_id, candidate_id, channel_context,
                       presence_at_ping, created_at, reach_request_id, channel, message_ts,
                       local_know_count, local_total_count, broadcast_closed,
                       expires_at, deleted_at
                FROM pings
                WHERE message_ts <> '' AND created_at < %s AND deleted_at IS NULL
                """,
                (cutoff,),
            )
            return [Ping(*row) for row in cursor.fetchall()]

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
    ) -> tuple[int, int, bool]:
        """Single-statement atomic increment and closure claim."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pings
                SET local_know_count = local_know_count + %s,
                    local_total_count = local_total_count + 1,
                    broadcast_closed = (
                        local_know_count + %s >= 3
                        OR local_total_count + 1 >= 5
                    )
                WHERE id = %s AND NOT broadcast_closed
                RETURNING local_know_count, local_total_count, broadcast_closed
                """,
                (
                    1 if counts_toward_known else 0,
                    1 if counts_toward_known else 0,
                    ping_id,
                ),
            )
            row = cursor.fetchone()
        self.connection.commit()
        if row is None:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT local_know_count, local_total_count FROM pings WHERE id = %s",
                    (ping_id,),
                )
                current = cursor.fetchone()
            return (int(current[0]), int(current[1]), False) if current else (0, 0, False)
        return int(row[0]), int(row[1]), bool(row[2])

    def record_broadcast_response(
        self, ping_id: str, responder_id: str, outcome: PingOutcome
    ) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO broadcast_responses
                  (id, ping_id, responder_id, outcome, responded_at, location)
                SELECT %s, %s, %s, %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM pings WHERE id = %s AND broadcast_closed
                )
                ON CONFLICT (ping_id, responder_id) DO NOTHING
                RETURNING id
                """,
                (
                    str(uuid4()), ping_id, responder_id, outcome.outcome,
                    outcome.responded_at, outcome.location, ping_id,
                ),
            )
            inserted = cursor.fetchone() is not None
        self.connection.commit()
        return inserted

    def broadcast_is_closed(self, ping_id: str) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT broadcast_closed FROM pings WHERE id = %s", (ping_id,))
            row = cursor.fetchone()
        return bool(row[0]) if row else False

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
                       local_know_count, local_total_count, broadcast_closed,
                       expires_at, deleted_at
                FROM pings
                WHERE reach_request_id = %s
                  AND message_ts <> ''
                  AND candidate_id <> ''
                  AND deleted_at IS NULL
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
