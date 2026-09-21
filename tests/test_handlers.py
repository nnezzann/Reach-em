from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

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
        self.ephemerals: list[tuple[str, str, str]] = []
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

    async def chat_postEphemeral(self, *, channel: str, user: str, text: str) -> None:
        self.ephemerals.append((channel, user, text))


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
    assert "Yo, Reach'em here, <@U-requester> needs a quick talk with  <@U-target>" in text
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


def test_submission_pushes_stage3_for_channel_scope() -> None:
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

    # Channel scope pushes Stage 3 (multi-channel picker) instead of sending;
    # channel selection is validated on the Stage 3 submit, never silently
    # falling back to "none".
    assert len(acknowledgements) == 1
    push_ack = acknowledgements[0]
    assert push_ack["response_action"] == "push"
    view = push_ack["view"]
    assert view["callback_id"] == "reach_stage3_submit"
    assert '"target_id":"U-target"' in view["private_metadata"]
    assert '"requester_id":"U-requester"' in view["private_metadata"]
    # The typed message is carried into Stage 3 so the requester keeps it.
    message_block = next(block for block in view["blocks"] if block["block_id"] == "message")
    assert message_block["element"]["initial_value"] == "Anyone seen them?"


def test_stage3_submit_requires_channels() -> None:
    app, _client = _register()
    acknowledgements: list[dict[str, Any]] = []

    async def ack(**kwargs: Any) -> None:
        acknowledgements.append(kwargs)

    asyncio.run(
        app.handlers["reach_stage3_submit"](
            ack=ack,
            body={"user": {"id": "U-requester"}},
            view={
                "private_metadata": json.dumps(
                    {"requester_id": "U-requester", "target_id": "U-target"}
                ),
                "state": {
                    "values": {
                        "broadcast_channels": {"channels_choice": {"selected_conversations": []}},
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
            "errors": {"broadcast_channels": "Pick at least one public channel."},
        }
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


def test_stage3_submit_launches_channel_broadcast() -> None:
    class Repo:
        def __init__(self) -> None:
            self.pings: list[str] = []
            self.contexts: list[Any] = []

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
            self.contexts.append(channel_context)
            return SimpleNamespace(id=f"ping-{len(self.pings)}")

        def set_ping_delivery(self, ping_id: str, channel: str, message_ts: str) -> None:
            pass

        def known_count(self, reach_request_id: str) -> int:
            return 0

    repo = Repo()
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    async def scenario() -> None:
        await app.handlers["reach_stage3_submit"](
            ack=ack,
            body={"user": {"id": "U-requester"}},
            view={
                "private_metadata": json.dumps(
                    {"requester_id": "U-requester", "target_id": "U-target"}
                ),
                "state": {
                    "values": {
                        "broadcast_channels": {
                            "channels_choice": {"selected_conversations": ["C1"]}
                        },
                        "message": {"message_input": {"value": "Anyone seen them?"}},
                    }
                },
            },
            client=client,
        )
        pending = [task for task in reach_bot.handlers._background_tasks if not task.done()]
        await asyncio.gather(*pending)

    asyncio.run(scenario())

    # Broadcast channel posts: one ping per channel, no pre-known candidate.
    assert repo.pings == [""]
    assert repo.contexts == ["broadcast"]
    assert client.sent[0]["channel"] == "C1"
    assert (
        "<!channel> Yo, Reach'em here, <@U-requester> needs a quick word with "
        "<@U-target> (s)He says:"
    ) in client.sent[0]["text"]
    assert client.sent[0]["text"].startswith("<!channel>")
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
    # channel_id rides through private_metadata for the broadcast ephemeral acks.
    assert json.loads(view["private_metadata"]) == {"ping_id": "ping-1", "channel_id": ""}
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
    assert client.sent[0]["text"] == (
        "Reply about <@U-target> from <@U-candidate>:\nThey're at lunch"
    )
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


# ---------------------------------------------------------------------------
# Pool 2: broadcast messages (candidate_id == "") — per-message local
# thresholds, entirely decoupled from the DM-side global counter.
# ---------------------------------------------------------------------------


def _broadcast_ping(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "id": "ping-b1",
        "reach_request_id": "req-1",
        "requester_id": "U-requester",
        "target_id": "U-target",
        "candidate_id": "",  # broadcast marker
        "channel": "C1",
        "message_ts": "1.0001",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class BroadcastRepo:
    def __init__(self, existing_outcome: Any = None, local: tuple[int, int] = (0, 0)) -> None:
        self.ping = _broadcast_ping()
        self.existing_outcome = existing_outcome
        self.local = local
        self.recorded: list[str] = []
        self.bumped: list[tuple[str, bool]] = []
        self.global_increments: list[str] = []
        self.broadcast_responses: set[str] = set()
        self.closed = False

    def get_ping(self, ping_id: str) -> Any:
        if ping_id != self.ping.id:
            return None
        # Return the stored local counts so closure checks reflect prior
        # responses on this message.
        self.ping = SimpleNamespace(
            **{
                **self.ping.__dict__,
                "local_know_count": self.local[0],
                "local_total_count": self.local[1],
            }
        )
        return self.ping

    def get_outcome(self, ping_id: str) -> Any:
        return self.existing_outcome

    def record_outcome(self, outcome: Any) -> None:
        self.recorded.append(outcome.outcome)

    def increment_broadcast_counts(
        self, ping_id: str, counts_toward_known: bool
    ) -> tuple[int, int, bool]:
        self.bumped.append((ping_id, counts_toward_known))
        know, total = self.local
        if self.closed:
            return know, total, False
        know = know + 1 if counts_toward_known else know
        self.local = (know, total + 1)
        closed = know >= 3 or self.local[1] >= 5
        self.closed = self.closed or closed
        return (*self.local, closed)

    def record_broadcast_response(self, ping_id: str, responder_id: str, outcome: Any) -> bool:
        key = f"{ping_id}:{responder_id}"
        if self.closed or key in self.broadcast_responses:
            return False
        self.broadcast_responses.add(key)
        self.recorded.append(outcome.outcome)
        return True

    def broadcast_is_closed(self, ping_id: str) -> bool:
        return self.closed

    def increment_known_count(self, reach_request_id: str) -> int:
        # A broadcast response must NEVER touch the DM global counter.
        self.global_increments.append(reach_request_id)
        return 99


def _submit_location_on_broadcast(app: Any, client: Any, channel_id: str = "C1") -> None:
    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["location_submit"](
            ack=ack,
            body={"user": {"id": "U-responder"}},
            view={
                "private_metadata": json.dumps({"ping_id": "ping-b1", "channel_id": channel_id}),
                "state": {
                    "values": {"location": {"location_input": {"value": "In the library"}}}
                },
            },
            client=client,
        )
    )


def test_broadcast_first_response_does_not_close_message() -> None:
    repo = BroadcastRepo()
    app, client = _register(repository=repo)

    _submit_location_on_broadcast(app, client)

    # Recorded, relayed, acked — but NO chat.update: a first response must
    # not close a broadcast message.
    assert repo.recorded == ["helped"]
    assert len(client.sent) == 1  # requester relay only
    assert client.message_updates == []


def test_broadcast_response_never_touches_dm_global_counter() -> None:
    repo = BroadcastRepo()
    app, client = _register(repository=repo)

    _submit_location_on_broadcast(app, client)

    assert repo.global_increments == []  # decoupling
    assert repo.bumped == [("ping-b1", True)]  # local counters instead


def test_broadcast_third_i_know_closes_message() -> None:
    repo = BroadcastRepo(local=(2, 2))
    app, client = _register(repository=repo)

    _submit_location_on_broadcast(app, client)

    # This response makes local_know_count 3 → closes with a single update.
    assert repo.recorded == ["helped"]
    assert [(u["channel"], u["ts"], u["text"]) for u in client.message_updates] == [
        ("C1", "1.0001", CUTOFF_STATUS)
    ]
    assert client.sent[-1]["channel"] == "U-requester"
    assert "3 \"I know\" response(s) and 3 total response(s)" in client.sent[-1]["text"]


def test_broadcast_fifth_total_response_closes_even_without_three_knows() -> None:
    # 2 knows + 2 others so far; this "I don't know" makes total 5 while
    # knows stay at 2 — the total cap closes the message regardless.
    repo = BroadcastRepo(local=(2, 4))
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["outcome_unknown"](
            ack=ack,
            body={
                "actions": [{"value": json.dumps({"ping_id": "ping-b1"})}],
                "user": {"id": "U-responder"},
                "channel_id": "C1",
            },
            client=client,
        )
    )

    assert repo.recorded == ["unknown"]
    assert repo.bumped == [("ping-b1", False)]
    assert [(u["channel"], u["ts"], u["text"]) for u in client.message_updates] == [
        ("C1", "1.0001", CUTOFF_STATUS)
    ]


def test_broadcast_early_responses_never_close_message() -> None:
    # 1 know + 2 others so far; another "I don't know" → (1, 3): below both
    # local thresholds, message must stay open.
    repo = BroadcastRepo(local=(1, 2))
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["outcome_unknown"](
            ack=ack,
            body={
                "actions": [{"value": json.dumps({"ping_id": "ping-b1"})}],
                "user": {"id": "U-responder"},
                "channel_id": "C1",
            },
            client=client,
        )
    )

    assert repo.recorded == ["unknown"]
    assert client.message_updates == []


def test_broadcast_responses_use_ephemeral_ack_not_visible_edit() -> None:
    repo = BroadcastRepo(local=(0, 1))
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["outcome_unknown"](
            ack=ack,
            body={
                "actions": [{"value": json.dumps({"ping_id": "ping-b1"})}],
                "user": {"id": "U-responder"},
                "channel_id": "C1",
            },
            client=client,
        )
    )

    # Acknowledgment is ephemeral, addressed to the responder.
    assert client.ephemerals == [("C1", "U-responder", "Thanks for your response!")]
    assert client.message_updates == []  # no in-place edit on a shared message


def test_broadcast_closed_message_click_gets_ephemeral_note_only() -> None:
    repo = BroadcastRepo(local=(2, 4))
    repo.closed = True
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    asyncio.run(
        app.handlers["outcome_unknown"](
            ack=ack,
            body={
                "actions": [{"value": json.dumps({"ping_id": "ping-b1"})}],
                "user": {"id": "U-responder"},
                "channel_id": "C1",
            },
            client=client,
        )
    )

    assert repo.recorded == []  # no duplicate recording
    assert client.message_updates == []  # never re-update a closed message
    assert client.ephemerals == [("C1", "U-responder", CUTOFF_STATUS)]


def test_broadcast_three_knows_closes_at_three_with_fewer_than_five_total() -> None:
    repo = BroadcastRepo()
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    async def respond(user_id: str) -> None:
        await app.handlers["location_submit"](
            ack=ack,
            body={"user": {"id": user_id}},
            view={
                "private_metadata": json.dumps({"ping_id": "ping-b1", "channel_id": "C1"}),
                "state": {"values": {"location": {"location_input": {"value": "Here"}}}},
            },
            client=client,
        )

    async def run_all() -> None:
        await asyncio.gather(respond("U1"), respond("U2"), respond("U3"))

    asyncio.run(run_all())

    assert repo.local == (3, 3)
    assert len(client.message_updates) == 1
    assert len([sent for sent in client.sent if sent["channel"] == "U-requester"]) == 4


def test_broadcast_five_total_closes_before_three_knows() -> None:
    repo = BroadcastRepo()
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    async def unknown(user_id: str) -> None:
        await app.handlers["outcome_unknown"](
            ack=ack,
            body={
                "actions": [{"value": json.dumps({"ping_id": "ping-b1"})}],
                "user": {"id": user_id},
                "channel_id": "C1",
            },
            client=client,
        )

    async def run_all() -> None:
        await asyncio.gather(*(unknown(f"U{i}") for i in range(1, 6)))

    asyncio.run(run_all())

    assert repo.local == (0, 5)
    assert len(client.message_updates) == 1
    assert "0 \"I know\" response(s) and 5 total response(s)" in client.sent[-1]["text"]


def test_broadcast_concurrent_responses_have_no_lost_increments_or_duplicate_close() -> None:
    repo = BroadcastRepo()
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    async def unknown(user_id: str) -> None:
        await app.handlers["outcome_unknown"](
            ack=ack,
            body={
                "actions": [{"value": json.dumps({"ping_id": "ping-b1"})}],
                "user": {"id": user_id},
                "channel_id": "C1",
            },
            client=client,
        )

    async def run_all() -> None:
        await asyncio.gather(*(unknown(f"U{i}") for i in range(1, 8)))

    asyncio.run(run_all())

    assert repo.local == (0, 5)
    assert len(client.message_updates) == 1


def test_broadcast_response_after_closure_does_not_increment_or_update_again() -> None:
    repo = BroadcastRepo(local=(2, 4))
    app, client = _register(repository=repo)

    async def ack(**kwargs: Any) -> None:
        pass

    async def unknown(user_id: str) -> None:
        await app.handlers["outcome_unknown"](
            ack=ack,
            body={
                "actions": [{"value": json.dumps({"ping_id": "ping-b1"})}],
                "user": {"id": user_id},
                "channel_id": "C1",
            },
            client=client,
        )

    asyncio.run(unknown("U1"))
    assert repo.local == (2, 5)
    assert len(client.message_updates) == 1
    asyncio.run(unknown("U2"))

    assert repo.local == (2, 5)
    assert len(client.message_updates) == 1
    assert client.ephemerals[-1] == ("C1", "U2", CUTOFF_STATUS)
