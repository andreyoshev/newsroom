"""The WhatsApp provider, backed by wacli (whatsmeow)."""

from __future__ import annotations

from datetime import datetime

from .. import wacli_client
from .base import Chat, Message


class WhatsAppProvider:
    type = "whatsapp"

    async def list_chats(self, state_dir: str) -> list[Chat]:
        raw = await wacli_client.list_chats(state_dir, limit=10000)
        out: list[Chat] = []
        for c in raw:
            jid = c.get("jid") or c.get("JID") or ""
            if not jid:
                continue
            out.append(
                Chat(
                    jid=jid,
                    name=c.get("name") or c.get("Name") or jid,
                    kind=c.get("kind") or ("group" if jid.endswith("@g.us") else "dm"),
                    last_message_ts=c.get("last_message_ts"),
                )
            )
        return out

    async def recent_messages(
        self, state_dir: str, after_iso: str, chat_jids: list[str]
    ) -> list[Message]:
        target = set(chat_jids)
        if not target:
            return []

        raw = await wacli_client.list_messages_since(state_dir, after_iso, limit=5000)
        out: list[Message] = []

        for m in raw:
            jid = m.get("ChatJID")
            if jid not in target:
                continue
            if m.get("ReactionEmoji") or m.get("Revoked") or m.get("DeletedForMe"):
                continue

            text = (m.get("Text") or "").strip()
            media = (m.get("MediaType") or "").lower()
            if media and not text:
                caption = (m.get("MediaCaption") or "").strip()
                text = f"[{media}] {caption}".strip()
            elif media and text:
                text = f"[{media}] {text}"
            if not text:
                continue

            ts_iso = m.get("Timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
                time_hhmm = ts.strftime("%H:%M")
            except (ValueError, AttributeError):
                time_hhmm = ""

            out.append(
                Message(
                    chat_jid=jid,
                    chat_name=m.get("ChatName") or jid,
                    sender_name=m.get("SenderName") or m.get("SenderJID", ""),
                    sender_id=m.get("SenderJID", ""),
                    timestamp_iso=ts_iso,
                    time_hhmm=time_hhmm,
                    text=text,
                    media_kind=media or None,
                )
            )

        out.sort(key=lambda x: x.timestamp_iso)
        return out
