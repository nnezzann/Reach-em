from reach_bot.handlers import is_direct_message_command, resolve_reach_target


def test_dm_command_accepts_explicit_channel_type() -> None:
    assert is_direct_message_command({"channel_type": "im", "channel_id": "C123"}) is True


def test_dm_command_accepts_slack_dm_channel_id_without_channel_type() -> None:
    assert is_direct_message_command({"channel_id": "D123"}) is True


def test_dm_command_rejects_public_channel() -> None:
    assert is_direct_message_command({"channel_type": "channel", "channel_id": "C123"}) is False


def test_resolves_slack_mention() -> None:
    assert resolve_reach_target("help me reach <@U123>", lambda value: value) == "U123"


def test_resolves_multiword_at_name() -> None:
    def resolve(value: str) -> str:
        if value == "Man of steel":
            return "U123"
        raise ValueError(value)

    assert resolve_reach_target("help me reach @Man of steel", resolve) == "U123"


def test_ignores_unrelated_at_name() -> None:
    assert resolve_reach_target("what is @Man of steel?", lambda value: value) is None
