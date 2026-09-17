from reach_bot.handlers import is_direct_message_command


def test_dm_command_accepts_explicit_channel_type() -> None:
    assert is_direct_message_command({"channel_type": "im", "channel_id": "C123"}) is True


def test_dm_command_accepts_slack_dm_channel_id_without_channel_type() -> None:
    assert is_direct_message_command({"channel_id": "D123"}) is True


def test_dm_command_rejects_public_channel() -> None:
    assert is_direct_message_command({"channel_type": "channel", "channel_id": "C123"}) is False
