import pytest

from reach_bot.nvidia import NvidiaClient, NvidiaClientError


@pytest.mark.asyncio
async def test_missing_key_is_user_actionable() -> None:
    with pytest.raises(NvidiaClientError, match="NVIDIA_API_KEY"):
        await NvidiaClient(None).complete([])


class FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return {"choices": [{"message": {"content": "hello"}}]}


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def post(self, *_args, **kwargs):
        self.headers = kwargs["headers"]
        return FakeResponse()


@pytest.mark.asyncio
async def test_success_parses_openai_compatible_response() -> None:
    session = FakeSession()
    client = NvidiaClient("secret", session=session)  # type: ignore[arg-type]

    assert await client.complete([{"role": "user", "content": "hi"}]) == "hello"
    assert session.headers == {"Authorization": "Bearer secret"}
