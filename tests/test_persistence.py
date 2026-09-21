from reach_bot.persistence import MemoryRepository, PingOutcome


def test_memory_increment_known_count_is_sequential() -> None:
    repo = MemoryRepository()
    request = repo.create_reach_request("U-requester", "U-target")

    assert [repo.increment_known_count(request.id) for _ in range(3)] == [1, 2, 3]


def test_memory_set_ping_delivery_and_unresponded_lookup() -> None:
    repo = MemoryRepository()
    request = repo.create_reach_request("U-requester", "U-target")
    answered = repo.create_ping(request.id, "U-requester", "U-target", "U-a", None, "active")
    open_ping = repo.create_ping(request.id, "U-requester", "U-target", "U-b", None, "offline")
    # Created without delivery info: must never appear in the sweep lookup.
    repo.create_ping(request.id, "U-requester", "U-target", "U-c", None, "offline")
    other_request = repo.create_reach_request("U-requester", "U-other")
    other = repo.create_ping(other_request.id, "U-requester", "U-other", "U-d", None, "offline")

    for ping, channel in ((answered, "C-a"), (open_ping, "C-b"), (other, "C-d")):
        repo.set_ping_delivery(ping.id, channel, f"{channel}-ts")
    repo.record_outcome(PingOutcome(answered.id, "helped"))

    assert [ping.id for ping in repo.get_unresponded_pings(request.id)] == [open_ping.id]
    delivered = repo.get_ping(answered.id)
    assert delivered is not None
    assert delivered.channel == "C-a"
    assert delivered.message_ts == "C-a-ts"


def test_memory_get_outcome_roundtrip() -> None:
    repo = MemoryRepository()
    request = repo.create_reach_request("U-requester", "U-target")
    ping = repo.create_ping(request.id, "U-requester", "U-target", "U-a", None, "active")

    assert repo.get_outcome(ping.id) is None
    repo.record_outcome(PingOutcome(ping.id, "helped", location="In the library"))
    outcome = repo.get_outcome(ping.id)

    assert outcome is not None
    assert outcome.outcome == "helped"
    assert outcome.location == "In the library"


def test_memory_broadcast_counts_are_per_message_and_independent() -> None:
    repo = MemoryRepository()
    request = repo.create_reach_request("U-requester", "U-target")
    ping_a = repo.create_ping(request.id, "U-requester", "U-target", "", "broadcast", "unknown")
    ping_b = repo.create_ping(request.id, "U-requester", "U-target", "", "broadcast", "unknown")

    # "I know" bumps both counters; other response types bump total only.
    assert repo.increment_broadcast_counts(ping_a.id, True) == (1, 1)
    assert repo.increment_broadcast_counts(ping_a.id, False) == (1, 2)
    # Each broadcast message owns its counters — no cross-talk, and the
    # request's global DM counter is untouched by any of this.
    assert repo.increment_broadcast_counts(ping_b.id, False) == (0, 1)
    assert repo.known_count(request.id) == 0


def test_memory_unresponded_pings_exclude_broadcast_pings() -> None:
    repo = MemoryRepository()
    request = repo.create_reach_request("U-requester", "U-target")
    dm = repo.create_ping(request.id, "U-requester", "U-target", "U-a", None, "active")
    broadcast = repo.create_ping(request.id, "U-requester", "U-target", "", "broadcast", "unknown")
    repo.set_ping_delivery(dm.id, "C-a", "ts-a")
    repo.set_ping_delivery(broadcast.id, "C1", "ts-1")

    # The DM global-threshold sweep may only ever see manual DM pings;
    # broadcast messages close via their own local thresholds instead.
    assert [ping.id for ping in repo.get_unresponded_pings(request.id)] == [dm.id]
