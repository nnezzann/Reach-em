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
