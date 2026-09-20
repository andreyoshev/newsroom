"""The source provider contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class Chat:
    jid: str
    name: str
    kind: str  # 'group' | 'dm' | ...
    last_message_ts: str | None = None


@dataclass
class Message:
    chat_jid: str
    chat_name: str
    sender_name: str
    sender_id: str
    timestamp_iso: str
    time_hhmm: str
    text: str
    media_kind: str | None = None


@runtime_checkable
class SourceProvider(Protocol):
    """A news source. Every method works within one `state_dir` — the
    isolated store belonging to a single user's link."""

    type: str

    async def list_chats(self, state_dir: str) -> list[Chat]:
        """Every chat this link can see, for the feed's source picker."""
        ...

    async def recent_messages(
        self, state_dir: str, after_iso: str, chat_jids: list[str]
    ) -> list[Message]:
        """Messages from the given chats since after_iso, oldest first."""
        ...
