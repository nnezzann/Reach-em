from reach_bot.conversation import ConversationStore


def test_history_is_bounded_by_messages_and_characters() -> None:
    store = ConversationStore(max_messages=3, max_characters=8)
    store.add("dm", "user", "1234")
    store.add("dm", "assistant", "5678")
    store.add("dm", "user", "90")

    assert [message.content for message in store.get("dm")] == ["5678", "90"]

