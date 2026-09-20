"""Source providers: where the news is pulled from.

Today there is exactly one, WhatsApp. Adding Telegram or Viber as a SOURCE
later means implementing `SourceProvider` and registering it in `PROVIDERS`.
The feeds and feed_sources tables are already provider-agnostic — chat_jid is
an opaque chat handle — so the schema does not have to change.
"""

from __future__ import annotations

from .base import Chat, Message, SourceProvider
from .whatsapp import WhatsAppProvider

PROVIDERS: dict[str, SourceProvider] = {
    "whatsapp": WhatsAppProvider(),
}


def get_provider(type_: str) -> SourceProvider:
    try:
        return PROVIDERS[type_]
    except KeyError:
        raise ValueError(f"unknown provider type: {type_}") from None


__all__ = ["Chat", "Message", "SourceProvider", "WhatsAppProvider", "PROVIDERS", "get_provider"]
