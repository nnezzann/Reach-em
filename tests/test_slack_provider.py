from reach_bot.slack_provider import SlackSignalProvider


class FakeClient:
    def users_list(self, limit: int):
        return {
            "members": [
                {
                    "id": "U123",
                    "name": "Ada",
                    "real_name": "Ada Lovelace",
                    "profile": {"display_name": "ada"},
                }
            ]
        }


def test_resolve_user_accepts_id_and_display_name() -> None:
    provider = SlackSignalProvider(FakeClient())

    assert provider.resolve_user("<@U123>") == "U123"
    assert provider.resolve_user("@ada") == "U123"
