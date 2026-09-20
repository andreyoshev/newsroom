"""The MCP server (FastMCP), scoped to a tenant by its OAuth token.

The guard below resolves a token into `Tenant(user_id, feed_id)` and puts it in
a contextvar. Each tool then looks the feed up by id and reads its sources, the
account's store and its Telegram destination straight from app.db.
"""

from __future__ import annotations

import contextvars
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastmcp import FastMCP
from starlette.types import ASGIApp, Receive, Scope, Send

from newsroom_shared import db as dbm
from newsroom_shared import oauth
from newsroom_shared.providers import get_provider
from newsroom_shared.telegram import TelegramClient

from .settings import base_url

log = logging.getLogger("newsroom.mcp")
logging.getLogger("httpx").setLevel(logging.WARNING)

_tenant: contextvars.ContextVar[oauth.Tenant | None] = contextvars.ContextVar(
    "tenant", default=None
)

mcp: FastMCP = FastMCP("newsroom")


async def _current_feed() -> tuple[Any, Any, list[Any]]:
    """(feed, connection, sources) for the feed this token is bound to."""
    tenant = _tenant.get()
    if tenant is None or not tenant.feed_id:
        raise ValueError("token is not bound to a feed — re-authorize and pick a feed")
    async with dbm.connect() as db:
        feed = await dbm.get_feed(db, tenant.feed_id)
        if feed is None:
            raise ValueError("feed no longer exists")
        conn = await dbm.get_connection_by_id(db, feed["connection_id"])
        sources = await dbm.list_feed_sources(db, tenant.feed_id)
    return feed, conn, sources


@mcp.tool
async def list_feed_sources() -> list[dict[str, str]]:
    """List the source chats in this feed (chat_jid + local display name).

    Use this to confirm which WhatsApp groups feed this Telegram digest before
    fetching messages.
    """
    _feed, _conn, sources = await _current_feed()
    return [
        {"chat_jid": s["chat_jid"], "name": s["local_name"] or s["chat_jid"]}
        for s in sources
    ]


@mcp.tool
async def fetch_recent_messages(since_hours: int = 24) -> list[dict[str, Any]]:
    """Return messages from this feed's source chats within the last `since_hours`.

    Reads the user's own WhatsApp store (isolated per account). Drops reactions,
    deleted and empty-body messages; sorted oldest first. Local display names
    configured for the feed override the raw chat name.

    Each item: `chat_jid`, `chat_name`, `sender_name`, `sender_id`,
    `timestamp_iso`, `time_hhmm`, `text`, `media_kind`.
    """
    if since_hours <= 0 or since_hours > 24 * 14:
        raise ValueError("since_hours must be between 1 and 336")

    feed, conn, sources = await _current_feed()
    if conn is None:
        raise ValueError("this feed has no source connection")

    after = (datetime.now(UTC) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    name_map = {s["chat_jid"]: s["local_name"] for s in sources if s["local_name"]}
    provider = get_provider(conn["type"])
    messages = await provider.recent_messages(
        conn["state_dir"], after, [s["chat_jid"] for s in sources]
    )
    return [
        {
            "chat_jid": m.chat_jid,
            "chat_name": name_map.get(m.chat_jid) or m.chat_name,
            "sender_name": m.sender_name,
            "sender_id": m.sender_id,
            "timestamp_iso": m.timestamp_iso,
            "time_hhmm": m.time_hhmm,
            "text": m.text,
            "media_kind": m.media_kind,
        }
        for m in messages
    ]


@mcp.tool
async def publish_to_telegram(text: str) -> dict[str, Any]:
    """Publish `text` to this feed's Telegram destination (parse_mode=HTML).

    Long messages are split into <=4096-char chunks at paragraph boundaries.
    The bot token and target chat come from the feed's configuration. Returns the
    Telegram `message_id`s created.
    """
    if not text.strip():
        raise ValueError("text must not be empty")
    feed, _conn, _sources = await _current_feed()
    if not feed["tg_bot_token"] or not feed["tg_chat_id"]:
        raise ValueError("this feed has no Telegram destination configured")
    async with TelegramClient(feed["tg_bot_token"]) as tg:
        message_ids = await tg.send_html(feed["tg_chat_id"], text)
    return {"chat_id": feed["tg_chat_id"], "message_ids": message_ids}


class TenantAuthASGI:
    """OAuth bearer guard in front of the mounted MCP application.

    A valid token becomes a Tenant in the contextvar and the request goes
    through. A missing or invalid one gets a 401 carrying
    `WWW-Authenticate: Bearer resource_metadata=...`, which is what starts
    Claude's OAuth flow (discovery -> registration -> authorize -> token).

    The metadata URL is built per request from the configured public base URL,
    so changing the domain on /settings needs no restart.
    """

    def __init__(self, inner: ASGIApp) -> None:
        self._inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return

        token = ""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                raw = value.decode("latin-1")
                if raw.lower().startswith("bearer "):
                    token = raw[7:].strip()
                break

        tenant = None
        if token:
            async with dbm.connect() as db:
                tenant = await oauth.verify_access_token(db, token)

        if tenant is None:
            base = await base_url(scope)
            await self._challenge(send, f"{base}/.well-known/oauth-protected-resource")
            return

        reset = _tenant.set(tenant)
        try:
            await self._inner(scope, receive, send)
        finally:
            _tenant.reset(reset)

    async def _challenge(self, send: Send, resource_metadata_url: str) -> None:
        header = f'Bearer resource_metadata="{resource_metadata_url}"'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", header.encode("latin-1")),
                ],
            }
        )
        await send(
            {"type": "http.response.body", "body": b'{"error":"invalid_token"}'}
        )
