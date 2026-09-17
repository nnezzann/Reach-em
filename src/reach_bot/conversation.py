from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConversationMessage:
    role: str
    content: str


class ConversationStore:
    """Bounded in-memory conversation history with a persistence-friendly API."""

    def __init__(self, max_messages: int = 20, max_characters: int = 12000) -> None:
        self.max_messages = max(1, max_messages)
        self.max_characters = max(1, max_characters)
        self._conversations: dict[str, deque[ConversationMessage]] = {}

    def get(self, key: str) -> list[ConversationMessage]:
        messages = list(self._conversations.get(key, ()))
        logger.debug("conversation history read messages=%d", len(messages))
        return messages

    def add(self, key: str, role: str, content: str) -> None:
        messages = self._conversations.setdefault(key, deque())
        messages.append(ConversationMessage(role, content))
        while len(messages) > self.max_messages or self._characters(messages) > self.max_characters:
            messages.popleft()
        logger.debug("conversation history updated messages=%d", len(messages))

    @staticmethod
    def _characters(messages: deque[ConversationMessage]) -> int:
        return sum(len(message.content) for message in messages)
