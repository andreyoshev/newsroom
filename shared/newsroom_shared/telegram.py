"""Telegram Bot API client. Each feed carries its own token, from @BotFather."""

from __future__ import annotations

import httpx

TELEGRAM_HARD_LIMIT = 4096
CHUNK_TARGET = 3900


class TelegramClient:
    def __init__(self, bot_token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{bot_token}",
            timeout=15.0,
        )

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_html(self, chat_id: str, text: str) -> list[int]:
        message_ids: list[int] = []
        for chunk in chunk_html(text):
            resp = await self._client.post(
                "/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
            message_ids.append(resp.json()["result"]["message_id"])
        return message_ids


def chunk_html(text: str) -> list[str]:
    if len(text) <= TELEGRAM_HARD_LIMIT:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > TELEGRAM_HARD_LIMIT:
        cut = remaining.rfind("\n\n", 0, CHUNK_TARGET)
        if cut == -1:
            cut = remaining.rfind("\n", 0, CHUNK_TARGET)
        if cut == -1:
            cut = remaining.rfind(" ", 0, CHUNK_TARGET)
        if cut == -1:
            cut = CHUNK_TARGET
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
