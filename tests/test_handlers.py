from __future__ import annotations

import asyncio
from typing import Any

from reach_bot.handlers import register_handlers
from reach_bot.ranking import RankedCandidates


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

    async def views_open(self, **kwargs: Any) -> None:
        self.opened.append(kwargs)

    async def views_update(self, **kwargs: Any) -> None:
        self.updated.append(kwargs)


class FakeRepository:
    pass


def _register(
    *,
    ranker: Any = lambda _target, _requester: RankedCandidates((), ()),
) -> tuple[FakeSlackApp, FakeClient]:
    app = FakeSlackApp()
    client = FakeClient()
    register_handlers(
        app,
        repository=FakeRepository(),  # type: ignore[arg-type]
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

    def ranker(target_id: str, requester_id: str) -> RankedCandidates:
        ranked_for.append((target_id, requester_id))
        return RankedCandidates((), ())

    app, client = _register(ranker=ranker)

    async def ack(**kwargs: Any) -> None:
        # Simulate the response_action="update" ack
        if kwargs.get("response_action") == "update":
            client.updated.append({"view": kwargs.get("view")})

    asyncio.run(
        app.handlers["reach_stage1_submit"](
            ack=ack,
            body={
                "user": {"id": "U-requester"},
                "view": {"id": "V1", "hash": "H1"},
            },
            view={
                "state": {
                    "values": {
                        "target": {
                            "target_user": {
                                "selected_user": "U-target"
                            }
                        }
                    }
                }
            },
            client=client,
        )
    )

    assert ranked_for == [("U-target", "U-requester")]
    assert client.updated[0]["view"]["callback_id"] == "reach_submit"
    assert '"target_id":"U-target"' in client.updated[0]["view"]["private_metadata"]


def test_target_selection_handles_exceptions() -> None:
    app, client = _register()
    acknowledgements: list[dict[str, Any]] = []

    async def ack(**kwargs: Any) -> None:
        acknowledgements.append(kwargs)

    # Test with missing view state to trigger exception
    asyncio.run(
        app.handlers["reach_stage1_submit"](
            ack=ack,
            body={
                "user": {"id": "U-requester"},
                "view": {"id": "V1", "hash": "H1"},
            },
            view={
                "state": {
                    "values": {}
                }
            },
            client=client,
        )
    )

    # Should handle exception gracefully and call ack()
    assert len(acknowledgements) == 1  # Ensure ack was called despite exception





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
