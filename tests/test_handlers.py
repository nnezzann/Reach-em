from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

import reach_bot.handlers
from reach_bot.handlers import register_handlers
from reach_bot.persistence import PingOutcome
from reach_bot.rendering import CUTOFF_STATUS, THANK_YOU_STATUS


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
        self.channel_members: dict[str, list[str]] = {}
        self.workspace_users: list[dict[str, Any]] = []

    async def views_open(self, **kwargs: Any) -> None:
        self.opened.append(kwargs)

    async def views_update(self, **kwargs: Any) -> None:
        self.updated.append(kwargs)

    async def users_info(self, *, user: str) -> dict[str, Any]:
        return {"user": {"profile": self.profiles.get(user, {})}}

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        return {"members": self.channel_members.get(str(kwargs.get("channel")), [])}

    async def users_list(self, **kwargs: Any) -> dict[str, Any]:
        return {"users": self.workspace_users}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        return {"channel": str(kwargs.get("channel", "")), "ts": f"{len(self.sent)}.0001"}

    async def chat_update(self, **kwargs: Any) -> None:
        self.message_updates.append(kwargs)


class FakeRepository:
    pass


def _register(*, repository: Any = None) -> tuple[FakeSlackApp, FakeClient]:
    app = FakeSlackApp()
    client = FakeClient()
    register_handlers(
        app,
        repository=repository or FakeRepository(),  # type: ignore[arg-type]
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


def test_target_selection_renders_stage_two_inside_the_ack() -> None:
    acknowledgements: list[dict[str, Any]] = []

    app, client = _register()
    client.profiles["U-target"] = {"display_name": "Grace"}

    async def ack(**kwargs: Any) -> None:
        acknowledgements.append(kwargs)

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

    assert len(acknowledgements) == 1
    update_ack = acknowledgements[0]
    assert update_ack["response_action"] == "update"
    view = update_ack["view"]
    assert view["callback_id"] == "reach_submit"
    assert '"target_id":"U-target"' in view["private_metadata"]
    assert '"target_name":"Grace"' in view["private_metadata"]
    # No loading view, no separate views_update round trip.
    assert "Finding the best people" not in json.dumps(view)
    assert client.updated == []
    assert [block["block_id"] for block in view["blocks"]] == [
        "candidates",
        "broadcast_scope",
        "message",
    ]
    message = next(block for block in view["blocks"] if block["block_id"] == "message")
    assert message["element"]["initial_value"].startswith("Have you seen Grace?")


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
    contexts: list[Any] = []
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
            channel_context: Any,
            presence: str,
        ) -> Any:
            created.append((reach_request_id, requester_id, target_id, candidate_id))
            contexts.append(channel_context)
            return ping

        def set_ping_delivery(self, ping_id: str, channel: str, message_ts: str) -> None:
            deliveries.append((ping_id, channel, message_ts))

    app, client = _register(repository=Repo())

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
                        "candidates": {"candidates": {"selected_users": ["U-active"]}},
                        "broadcast_scope": {
                            "scope_choice": {"selected_option": {"value": "none"}}
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
    # Suggestions are dormant: no computed channel context is stored.
    assert contexts == [None]
    assert deliveries == [("ping-1", "U-active", "1.0001")]
    assert not reach_bot.handlers._background_tasks


def test_submission_rejects_empty_candidates_with_none_scope() -> None:
    app, _client = _register()
    acknowledgements: list[dict[str, Any]] = []

    async def ack(**kwargs: Any) -> None:
        acknowledgements.append(kwargs)

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
                        "candidates": {"candidates": {"selected_users": []}},
                        "broadcast_scope": {
                            "scope_choice": {"selected_option": {"value": "none"}}
                        },
                        "message": {"message_input": {"value": "Anyone seen them?"}},
                    }
                },
            },
            client=FakeClient(),
        )
    )

    assert acknowledgements == [
        {
            "response_action": "errors",
            "errors": {
                "candidates": "Pick at least one person, or choose a broadcast option below."
            },
        }
    ]


def test_submission_requires_channel_for_channel_scope() -> None:
    app, _client = _register()
    acknowledgements: list[dict[str, Any]] = []

    async def ack(**kwargs: Any) -> None:
        acknowledgements.append(kwargs)

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
                        "candidates": {"candidates": {"selected_users": []}},
                        "broadcast_scope": {
                            "scope_choice": {"selected_option": {"value": "channel"}}
                        },
                        "message": {"message_input": {"value": "Anyone seen them?"}},
                    }
                },
            },
            client=FakeClient(),
        )
    )

    assert acknowledgements == [
        {"response_action": "errors", "errors": {"broadcast_channel": "Pick a channel."}}
    ]


def test_scope_choice_adds_channel_picker_and_preserves_input() -> None:
    app, client = _register()

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["scope_choice"](
            ack=ack,
            body={
                "view": {
                    "id": "V1",
                    "private_metadata": json.dumps(
                        {
                            "requester_id": "U-requester",
                            "target_id": "U-target",
                            "target_name": "Grace",
                        }
                    ),
                    "state": {
                        "values": {
                            "candidates": {"candidates": {"selected_users": ["U-friend"]}},
                            "broadcast_scope": {
                                "scope_choice": {"selected_option": {"value": "channel"}}
                            },
                            "message": {"message_input": {"value": "Edited message"}},
                        }
                    },
                }
            },
            client=client,
        )
    )

    assert client.updated[0]["view_id"] == "V1"
    blocks = client.updated[0]["view"]["blocks"]
    assert [block["block_id"] for block in blocks] == [
        "candidates",
        "broadcast_scope",
        "broadcast_channel",
        "message",
    ]
    assert blocks[0]["element"]["initial_users"] == ["U-friend"]
    assert blocks[1]["element"]["initial_option"]["value"] == "channel"
    assert blocks[2]["element"]["type"] == "conversations_select"
    assert blocks[2]["element"]["filter"]["include"] == ["public", "private"]
    assert "initial_conversation" not in blocks[2]["element"]
    assert blocks[3]["element"]["initial_value"] == "Edited message"


def test_scope_choice_keeps_picked_channel_on_rerender() -> None:
    app, client = _register()

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["scope_choice"](
            ack=ack,
            body={
                "view": {
                    "id": "V1",
                    "private_metadata": json.dumps(
                        {
                            "requester_id": "U-requester",
                            "target_id": "U-target",
                            "target_name": "Grace",
                        }
                    ),
                    "state": {
                        "values": {
                            "candidates": {"candidates": {"selected_users": []}},
                            "broadcast_scope": {
                                "scope_choice": {"selected_option": {"value": "channel"}}
                            },
                            "broadcast_channel": {
                                "channel_choice": {"selected_conversation": "C1"}
                            },
                            "message": {"message_input": {"value": "Edited message"}},
                        }
                    },
                }
            },
            client=client,
        )
    )

    blocks = client.updated[0]["view"]["blocks"]
    channel = next(block for block in blocks if block["block_id"] == "broadcast_channel")
    assert channel["element"]["initial_conversation"] == "C1"


def test_scope_choice_removes_channel_picker_when_scope_is_none() -> None:
    app, client = _register()

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["scope_choice"](
            ack=ack,
            body={
                "view": {
                    "id": "V1",
                    "private_metadata": json.dumps(
                        {
                            "requester_id": "U-requester",
                            "target_id": "U-target",
                            "target_name": "Grace",
                        }
                    ),
                    "state": {
                        "values": {
                            "candidates": {"candidates": {"selected_users": []}},
                            "broadcast_scope": {
                                "scope_choice": {"selected_option": {"value": "none"}}
                            },
                            "broadcast_channel": {
                                "channel_choice": {"selected_conversation": "C1"}
                            },
                            "message": {"message_input": {"value": "Edited message"}},
                        }
                    },
                }
            },
            client=client,
        )
    )

    blocks = client.updated[0]["view"]["blocks"]
    assert [block["block_id"] for block in blocks] == ["candidates", "broadcast_scope", "message"]


def test_submission_launches_broadcast_fan_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from reach_bot import broadcast

    monkeypatch.setattr(broadcast, "SEND_DELAY_SECONDS", 0.0)

    class Repo:
        def __init__(self) -> None:
            self.pings: list[str] = []

        def create_reach_request(self, requester_id: str, target_id: str) -> Any:
            return SimpleNamespace(id="request-1", requester_id=requester_id, target_id=target_id)

        def create_ping(
            self,
            reach_request_id: str,
            requester_id: str,
            target_id: str,
            candidate_id: str,
            channel_context: Any,
            presence: str,
        ) -> Any:
            self.pings.append(candidate_id)
            return SimpleNamespace(id=f"ping-{len(self.pings)}")

        def set_ping_delivery(self, ping_id: str, channel: str, message_ts: str) -> None:
            pass

        def known_count(self, reach_request_id: str) -> int:
            return 0

    repo = Repo()
    app, client = _register(repository=repo)
    client.channel_members["C1"] = ["U-target", "U-requester", "U-broadcast"]

    async def ack(**kwargs: Any) -> None:
        pass

    async def scenario() -> None:
        await app.handlers["reach_submit"](
            ack=ack,
            body={"user": {"id": "U-requester"}},
            view={
                "private_metadata": json.dumps(
                    {"requester_id": "U-requester", "target_id": "U-target"}
                ),
                "state": {
                    "values": {
                        "candidates": {"candidates": {"selected_users": []}},
                        "broadcast_scope": {
                            "scope_choice": {"selected_option": {"value": "channel"}}
                        },
                        "broadcast_channel": {
                            "channel_choice": {"selected_conversation": "C1"}
                        },
                        "message": {"message_input": {"value": "Anyone seen them?"}},
                    }
                },
            },
            client=client,
        )
        pending = [
            task for task in reach_bot.handlers._background_tasks if not task.done()
        ]
        await asyncio.gather(*pending)

    asyncio.run(scenario())

    assert repo.pings == ["U-broadcast"]
    assert client.sent[0]["channel"] == "U-broadcast"
    assert "Reach, relaying for <@U-requester> about <@U-target>" in client.sent[0]["text"]
    actions = client.sent[0]["blocks"][1]["elements"]
    assert [element["action_id"] for element in actions] == [
        "outcome_helped",
        "outcome_unknown",
        "outcome_more",
    ]


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


class UnknownRepo:
    def __init__(self, existing_outcome: PingOutcome | None = None) -> None:
        self.ping = SimpleNamespace(
            id="ping-1",
            reach_request_id="req-1",
            requester_id="U-requester",
            target_id="U-target",
            candidate_id="U-candidate",
            channel="C-candidate",
            message_ts="1.0001",
        )
        self.existing_outcome = existing_outcome
        self.recorded: list[str] = []

    def get_ping(self, ping_id: str) -> Any:
        return self.ping if ping_id == "ping-1" else None

    def get_outcome(self, ping_id: str) -> PingOutcome | None:
        return self.existing_outcome

    def record_outcome(self, outcome: PingOutcome) -> None:
        self.recorded.append(outcome.outcome)


class ReplyRepo:
    def __init__(self, existing_outcome: PingOutcome | None = None) -> None:
        self.ping = SimpleNamespace(
            id="ping-1",
            reach_request_id="req-1",
            requester_id="U-requester",
            target_id="U-target",
            candidate_id="U-candidate",
            channel="C-candidate",
            message_ts="1.0001",
        )
        self.existing_outcome = existing_outcome
        self.recorded: list[str] = []

    def get_ping(self, ping_id: str) -> Any:
        return self.ping if ping_id == "ping-1" else None

    def get_outcome(self, ping_id: str) -> PingOutcome | None:
        return self.existing_outcome

    def record_outcome(self, outcome: PingOutcome) -> None:
        self.recorded.append(outcome.outcome)


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
    # Cleanup is now applied to the responder's own message
    assert len(client.message_updates) == 1
    assert client.message_updates[0]["text"] == THANK_YOU_STATUS


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

    # First update is cleanup of responder's own message, then cutoff sweep
    assert [(update["channel"], update["ts"]) for update in client.message_updates] == [
        ("C-candidate", "1.0001"),  # responder's own cleanup
        ("C-other", "2.0001"),     # cutoff sweep
        ("C-other2", "3.0001"),   # cutoff sweep
    ]
    assert client.message_updates[0]["text"] == THANK_YOU_STATUS
    assert all(update["text"] == CUTOFF_STATUS for update in client.message_updates[1:])


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


def test_already_answered_ping_shows_cutoff_status() -> None:
    """I know with existing outcome shows cutoff status (special case for race condition)."""
    repo = LocationRepo(existing_outcome=PingOutcome("ping-1", "helped"))
    app, client = _register(repository=repo)

    _submit_location(app, client)

    assert repo.incremented == []
    assert repo.recorded == []
    assert client.sent == []
    assert len(client.message_updates) == 1
    assert client.message_updates[0]["text"] == CUTOFF_STATUS


def test_outcome_unknown_applies_cleanup() -> None:
    repo = UnknownRepo()
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["outcome_unknown"](
            ack=ack,
            body={
                "actions": [{"value": json.dumps({"ping_id": "ping-1"})}],
                "user": {"id": "U-candidate"},
            },
            client=client,
        )
    )

    assert repo.recorded == ["unknown"]
    assert len(client.message_updates) == 1
    assert client.message_updates[0]["text"] == THANK_YOU_STATUS


def test_outcome_unknown_duplicate_click_shows_ephemeral() -> None:
    repo = UnknownRepo()
    repo.existing_outcome = "unknown"
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["outcome_unknown"](
            ack=ack,
            body={
                "actions": [{"value": json.dumps({"ping_id": "ping-1"})}],
                "user": {"id": "U-candidate"},
            },
            client=client,
        )
    )

    assert repo.recorded == []  # No duplicate recording
    assert len(client.sent) == 1
    assert client.sent[0]["text"] == "You've already responded to this — thanks!"
    assert client.message_updates == []  # No cleanup on duplicate


def test_reply_more_submit_applies_cleanup() -> None:
    repo = ReplyRepo()
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["reply_more_submit"](
            ack=ack,
            body={"user": {"id": "U-candidate"}},
            view={
                "private_metadata": "ping-1",
                "state": {"values": {"reply": {"reply_input": {"value": "They're at lunch"}}}},
            },
            client=client,
        )
    )

    assert repo.recorded == ["replied"]
    assert len(client.sent) == 1
    assert client.sent[0]["text"] == "Reply from Reach recipient:\nThey're at lunch"
    assert len(client.message_updates) == 1
    assert client.message_updates[0]["text"] == THANK_YOU_STATUS


def test_reply_more_duplicate_click_shows_ephemeral() -> None:
    repo = ReplyRepo()
    repo.existing_outcome = "replied"
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["reply_more_submit"](
            ack=ack,
            body={"user": {"id": "U-candidate"}},
            view={
                "private_metadata": "ping-1",
                "state": {"values": {"reply": {"reply_input": {"value": "They're at lunch"}}}},
            },
            client=client,
        )
    )

    assert repo.recorded == []  # No duplicate recording
    assert len(client.sent) == 1
    assert client.sent[0]["text"] == "You've already responded to this — thanks!"
    assert client.message_updates == []  # No cleanup on duplicate
