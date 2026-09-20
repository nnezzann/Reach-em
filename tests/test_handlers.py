from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from reach_bot.handlers import register_handlers
from reach_bot.persistence import PingOutcome
from reach_bot.ranking import Candidate, RankedCandidates
from reach_bot.rendering import CUTOFF_STATUS


class FakeSlackApp:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def command(self, name: str) -> Any:
        return self._capture(name)

    def action(self, name: str) -> Any:
        return self._capture(name)

    def view(self, name: str) -> Any:
        return self._capture(name)

    def _capture(self, name: str) -> Any:
        def decorator(handler: Any) -> Any:
            self.handlers[name] = handler
            return handler

        return decorator


class FakeClient:
    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.message_updates: list[dict[str, Any]] = []
        self.profiles: dict[str, dict[str, Any]] = {}

    async def views_open(self, **kwargs: Any) -> None:
        self.opened.append(kwargs)

    async def views_update(self, **kwargs: Any) -> None:
        self.updated.append(kwargs)

    async def users_info(self, *, user: str) -> dict[str, Any]:
        return {"user": {"profile": self.profiles.get(user, {})}}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        return {"channel": str(kwargs.get("channel", "")), "ts": f"{len(self.sent)}.0001"}

    async def chat_update(self, **kwargs: Any) -> None:
        self.message_updates.append(kwargs)


class FakeRepository:
    pass


def _register(
    *,
    ranker: Any = lambda _target, _requester: RankedCandidates((), ()),
    repository: Any = None,
) -> tuple[FakeSlackApp, FakeClient]:
    app = FakeSlackApp()
    client = FakeClient()
    register_handlers(
        app,
        repository=repository or FakeRepository(),  # type: ignore[arg-type]
        ranker=ranker,
        renderer=lambda _ranked: [],
    )
    return app, client


def test_bare_reach_command_opens_initial_modal() -> None:
    app, client = _register()
    acknowledgements: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []

    async def ack(**kwargs: Any) -> None:
        acknowledgements.append(kwargs)

    async def respond(**kwargs: Any) -> None:
        responses.append(kwargs)

    asyncio.run(
        app.handlers["/reach"](
            ack=ack,
            command={"trigger_id": "trigger", "text": ""},
            respond=respond,
            client=client,
        )
    )

    assert acknowledgements == [{}]
    assert responses == []
    assert client.opened[0]["trigger_id"] == "trigger"
    assert client.opened[0]["view"]["callback_id"] == "reach_stage1_submit"
    assert client.opened[0]["view"]["blocks"][0]["element"]["action_id"] == "target_user"


def test_target_selection_resolves_target_before_stage_two() -> None:
    ranked_for: list[tuple[str, str]] = []
    acknowledgements: list[dict[str, Any]] = []

    def ranker(target_id: str, requester_id: str) -> RankedCandidates:
        ranked_for.append((target_id, requester_id))
        return RankedCandidates((), ())

    app, client = _register(ranker=ranker)

    async def ack(**kwargs: Any) -> None:
        acknowledgements.append(kwargs)

    asyncio.run(
        app.handlers["reach_stage1_submit"](
            ack=ack,
            body={"user": {"id": "U-requester"}},
            view={
                "id": "V1",
                "hash": "H1",
                "state": {
                    "values": {
                        "target": {"target_user": {"selected_user": "U-target"}}
                    }
                },
            },
            client=client,
        )
    )

    assert ranked_for == [("U-target", "U-requester")]
    assert len(acknowledgements) == 1
    update_ack = acknowledgements[0]
    assert update_ack["response_action"] == "update"
    assert update_ack["view"]["callback_id"] == "reach_stage1_submit"
    assert "Finding the best people" in update_ack["view"]["blocks"][0]["text"]["text"]
    assert client.updated[0]["view_id"] == "V1"
    assert client.updated[0]["view"]["callback_id"] == "reach_submit"
    assert '"target_id":"U-target"' in client.updated[0]["view"]["private_metadata"]








def test_stage_two_shows_resolved_names_for_candidates_and_target() -> None:
    def ranker(target_id: str, requester_id: str) -> RankedCandidates:
        return RankedCandidates((Candidate("U-active", presence="active"),), ())

    app, client = _register(ranker=ranker)
    client.profiles["U-active"] = {"display_name": "Ada"}
    client.profiles["U-target"] = {"display_name": "Grace"}

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["reach_stage1_submit"](
            ack=ack,
            body={"user": {"id": "U-requester"}},
            view={
                "id": "V1",
                "state": {"values": {"target": {"target_user": {"selected_user": "U-target"}}}},
            },
            client=client,
        )
    )

    stage2 = client.updated[0]["view"]
    checkboxes = next(
        block for block in stage2["blocks"] if block["element"]["type"] == "checkboxes"
    )
    assert checkboxes["element"]["options"][0]["text"]["text"] == "Ada"
    message_input = next(
        block for block in stage2["blocks"] if block["element"]["type"] == "plain_text_input"
    )
    assert message_input["element"]["initial_value"].startswith("Have you seen Grace?")


def test_submission_rejects_missing_target_metadata() -> None:
    app, _client = _register()
    acknowledgements: list[dict[str, Any]] = []

    async def ack(**kwargs: Any) -> None:
        acknowledgements.append(kwargs)

    asyncio.run(
        app.handlers["reach_submit"](
            ack=ack,
            body={"user": {"id": "U-requester"}},
            view={"state": {"values": {}}, "private_metadata": "{}"},
            client=FakeClient(),
        )
    )

    assert acknowledgements == [
        {
            "response_action": "errors",
            "errors": {"message": "Select who you are trying to reach first."},
        }
    ]


def test_submission_sends_message_to_selected_recipients() -> None:
    ping = SimpleNamespace(id="ping-1")
    created: list[tuple[str, str, str, str]] = []
    deliveries: list[tuple[str, str, str]] = []

    class Repo:
        def create_reach_request(self, requester_id: str, target_id: str) -> Any:
            return SimpleNamespace(id="request-1", requester_id=requester_id, target_id=target_id)

        def create_ping(
            self,
            reach_request_id: str,
            requester_id: str,
            target_id: str,
            candidate_id: str,
            evidence: Any,
            presence: str,
        ) -> Any:
            created.append((reach_request_id, requester_id, target_id, candidate_id))
            return ping

        def set_ping_delivery(self, ping_id: str, channel: str, message_ts: str) -> None:
            deliveries.append((ping_id, channel, message_ts))

    def ranker(target_id: str, requester_id: str) -> RankedCandidates:
        return RankedCandidates((Candidate("U-active", presence="active"),), ())

    app, client = _register(ranker=ranker, repository=Repo())

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["reach_submit"](
            ack=ack,
            body={"user": {"id": "U-requester"}},
            view={
                "private_metadata": json.dumps(
                    {"requester_id": "U-requester", "target_id": "U-target"}
                ),
                "state": {
                    "values": {
                        "suggested_active_now": {
                            "suggested_candidates": {
                                "selected_options": [{"value": "U-active"}]
                            }
                        },
                        "message": {"message_input": {"value": "Anyone seen them?"}},
                    }
                },
            },
            client=client,
        )
    )

    assert len(client.sent) == 1
    text = client.sent[0]["text"]
    assert "Reach, relaying for <@U-requester> about <@U-target>" in text
    assert "Anyone seen them?" in text
    assert client.sent[0]["blocks"][0]["type"] == "section"
    actions = client.sent[0]["blocks"][1]["elements"]
    assert [element["action_id"] for element in actions] == [
        "outcome_helped",
        "outcome_unknown",
        "outcome_more",
    ]
    assert json.loads(actions[0]["value"])["ping_id"] == "ping-1"
    assert created == [("request-1", "U-requester", "U-target", "U-active")]
    assert deliveries == [("ping-1", "U-active", "1.0001")]


def test_i_know_click_opens_single_line_location_modal() -> None:
    app, client = _register()

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["outcome_helped"](
            ack=ack,
            body={
                "trigger_id": "trigger",
                "actions": [{"value": json.dumps({"ping_id": "ping-1"})}],
            },
            client=client,
        )
    )

    view = client.opened[0]["view"]
    assert view["callback_id"] == "location_submit"
    assert view["private_metadata"] == "ping-1"
    block = view["blocks"][0]
    assert block["block_id"] == "location"
    assert block["element"]["action_id"] == "location_input"
    assert block["element"]["multiline"] is False
    assert block["element"]["placeholder"]["text"] == "Where or how can they reach them?"


class LocationRepo:
    def __init__(self, count: int = 1, existing_outcome: PingOutcome | None = None) -> None:
        self.ping = SimpleNamespace(
            id="ping-1",
            reach_request_id="req-1",
            requester_id="U-requester",
            target_id="U-target",
            candidate_id="U-candidate",
            channel="C-candidate",
            message_ts="1.0001",
        )
        self.count = count
        self.existing_outcome = existing_outcome
        self.open_pings: list[Any] = []
        self.recorded: list[PingOutcome] = []
        self.incremented: list[str] = []

    def get_ping(self, ping_id: str) -> Any:
        return self.ping if ping_id == "ping-1" else None

    def get_outcome(self, ping_id: str) -> PingOutcome | None:
        return self.existing_outcome

    def increment_known_count(self, reach_request_id: str) -> int:
        self.incremented.append(reach_request_id)
        return self.count

    def record_outcome(self, outcome: PingOutcome) -> None:
        self.recorded.append(outcome)

    def get_unresponded_pings(self, reach_request_id: str) -> list[Any]:
        return self.open_pings


def _submit_location(app: Any, client: Any) -> None:
    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["location_submit"](
            ack=ack,
            body={"user": {"id": "U-candidate"}},
            view={
                "private_metadata": "ping-1",
                "state": {
                    "values": {"location": {"location_input": {"value": "In the library"}}}
                },
            },
            client=client,
        )
    )


def test_location_submit_records_outcome_and_dms_requester() -> None:
    repo = LocationRepo()
    app, client = _register(repository=repo)

    _submit_location(app, client)

    assert [outcome.outcome for outcome in repo.recorded] == ["helped"]
    assert repo.recorded[0].location == "In the library"
    assert len(client.sent) == 1
    assert client.sent[0]["channel"] == "U-requester"
    text = client.sent[0]["text"]
    assert "<@U-candidate>" in text
    assert "<@U-target>" in text
    assert "In the library" in text
    assert client.message_updates == []


def test_third_i_know_sweeps_other_open_messages() -> None:
    repo = LocationRepo(count=3)
    repo.open_pings = [
        repo.ping,
        SimpleNamespace(id="ping-2", channel="C-other", message_ts="2.0001"),
        SimpleNamespace(id="ping-3", channel="C-other2", message_ts="3.0001"),
        SimpleNamespace(id="ping-4", channel="C-nodelivery", message_ts=""),
    ]
    app, client = _register(repository=repo)

    _submit_location(app, client)

    assert [(update["channel"], update["ts"]) for update in client.message_updates] == [
        ("C-other", "2.0001"),
        ("C-other2", "3.0001"),
    ]
    assert all(update["text"] == CUTOFF_STATUS for update in client.message_updates)


def test_fourth_i_know_shows_status_without_recording() -> None:
    repo = LocationRepo(count=4)
    app, client = _register(repository=repo)

    _submit_location(app, client)

    assert repo.recorded == []
    assert repo.incremented == ["req-1"]
    assert client.sent == []
    assert [(u["channel"], u["ts"], u["text"]) for u in client.message_updates] == [
        ("C-candidate", "1.0001", CUTOFF_STATUS)
    ]


def test_already_answered_ping_is_not_counted_or_recorded_twice() -> None:
    repo = LocationRepo(existing_outcome=PingOutcome("ping-1", "helped"))
    app, client = _register(repository=repo)

    _submit_location(app, client)

    assert repo.incremented == []
    assert repo.recorded == []
    assert client.sent == []
    assert len(client.message_updates) == 1
