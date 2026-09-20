"""The app database (SQLite), separate from the wacli message stores.

It sits on the shared `wacli-state` volume (`/wacli-state/app.db`) and is
opened by two processes: web (read/write) and the supervisor (read). Hence
WAL — it keeps readers off the writer's back without long locks.

Access is `aiosqlite` plus plain SQL, no ORM: there are few tables and the app
is small and async. Queries needed by more than one service live here;
narrowly scoped ones stay in the web routers.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import aiosqlite

APP_DB_PATH = os.environ.get("APP_DB", "/wacli-state/app.db")

# --- time -------------------------------------------------------------------


def iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_in(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_expired(iso_ts: str | None) -> bool:
    if not iso_ts:
        return True
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(UTC) >= ts


# --- connection --------------------------------------------------------------


@asynccontextmanager
async def connect(path: str | None = None) -> AsyncIterator[aiosqlite.Connection]:
    """Open a connection with foreign keys, WAL and row_factory=Row."""
    db = await aiosqlite.connect(path or APP_DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        yield db
    finally:
        await db.close()


SCHEMA = """
-- Runtime configuration; see newsroom_shared.settings_store.
CREATE TABLE IF NOT EXISTS app_settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS provider_connections (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  type         TEXT NOT NULL,            -- 'whatsapp' | future 'telegram'/'viber'
  state_dir    TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'unlinked',  -- unlinked|pairing|linked|error
  display      TEXT,
  last_sync_at TEXT,
  updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(user_id, type)
);

CREATE TABLE IF NOT EXISTS feeds (
  id            TEXT PRIMARY KEY,
  user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  connection_id TEXT NOT NULL REFERENCES provider_connections(id) ON DELETE CASCADE,
  tg_bot_token  TEXT NOT NULL DEFAULT '',
  tg_chat_id    TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feed_sources (
  id         TEXT PRIMARY KEY,
  feed_id    TEXT NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
  chat_jid   TEXT NOT NULL,
  local_name TEXT,
  UNIQUE(feed_id, chat_jid)
);

-- OAuth 2.1 authorization server -------------------------------------------
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id     TEXT PRIMARY KEY,
  client_name   TEXT,
  redirect_uris TEXT NOT NULL,           -- JSON array
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
  code           TEXT PRIMARY KEY,
  client_id      TEXT NOT NULL,
  user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  feed_id        TEXT REFERENCES feeds(id) ON DELETE CASCADE,
  redirect_uri   TEXT NOT NULL,
  code_challenge TEXT NOT NULL,
  expires_at     TEXT NOT NULL,
  used           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
  access_token      TEXT PRIMARY KEY,
  refresh_token     TEXT UNIQUE,
  client_id         TEXT NOT NULL,
  user_id           TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  feed_id           TEXT REFERENCES feeds(id) ON DELETE CASCADE,
  access_expires_at TEXT NOT NULL,
  refresh_expires_at TEXT,
  revoked           INTEGER NOT NULL DEFAULT 0,
  last_used_at      TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def migrate(path: str | None = None) -> None:
    """Create the schema and seed settings. Idempotent — runs on every start."""
    from . import settings_store  # imported here: settings_store imports this module

    async with connect(path) as db:
        await db.executescript(SCHEMA)
        await settings_store.bootstrap(db)
        await settings_store.commit(db)


# --- users -------------------------------------------------------------------


async def get_user_by_email(db: aiosqlite.Connection, email: str) -> aiosqlite.Row | None:
    cur = await db.execute("SELECT * FROM users WHERE email = ?", (email.lower(),))
    return await cur.fetchone()


async def get_user_by_id(db: aiosqlite.Connection, user_id: str) -> aiosqlite.Row | None:
    cur = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return await cur.fetchone()


async def insert_user(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    email: str,
    password_hash: str,
    is_admin: bool = False,
) -> None:
    await db.execute(
        "INSERT INTO users (id, email, password_hash, is_admin) VALUES (?, ?, ?, ?)",
        (user_id, email.lower(), password_hash, 1 if is_admin else 0),
    )


async def count_users(db: aiosqlite.Connection) -> int:
    cur = await db.execute("SELECT COUNT(*) AS n FROM users")
    row = await cur.fetchone()
    return int(row["n"])


async def list_users(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cur = await db.execute("SELECT * FROM users ORDER BY created_at, email")
    return list(await cur.fetchall())


async def delete_user(db: aiosqlite.Connection, user_id: str) -> None:
    """Remove the account. Sessions, connections, feeds and tokens cascade."""
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))


async def set_admin(db: aiosqlite.Connection, user_id: str, is_admin: bool) -> None:
    await db.execute(
        "UPDATE users SET is_admin = ? WHERE id = ?", (1 if is_admin else 0, user_id)
    )


# --- sessions ----------------------------------------------------------------


async def insert_session(
    db: aiosqlite.Connection, *, token: str, user_id: str, expires_at: str
) -> None:
    await db.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires_at),
    )


async def get_session_user(db: aiosqlite.Connection, token: str) -> aiosqlite.Row | None:
    cur = await db.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token = ? AND s.expires_at > ?""",
        (token, iso_now()),
    )
    return await cur.fetchone()


async def delete_session(db: aiosqlite.Connection, token: str) -> None:
    await db.execute("DELETE FROM sessions WHERE token = ?", (token,))


# --- provider connections ----------------------------------------------------


async def get_connection(
    db: aiosqlite.Connection, user_id: str, type_: str = "whatsapp"
) -> aiosqlite.Row | None:
    cur = await db.execute(
        "SELECT * FROM provider_connections WHERE user_id = ? AND type = ?",
        (user_id, type_),
    )
    return await cur.fetchone()


async def get_connection_by_id(
    db: aiosqlite.Connection, connection_id: str
) -> aiosqlite.Row | None:
    cur = await db.execute(
        "SELECT * FROM provider_connections WHERE id = ?", (connection_id,)
    )
    return await cur.fetchone()


async def upsert_connection(
    db: aiosqlite.Connection,
    *,
    conn_id: str,
    user_id: str,
    type_: str,
    state_dir: str,
    status: str = "unlinked",
) -> None:
    await db.execute(
        """INSERT INTO provider_connections (id, user_id, type, state_dir, status)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id, type) DO NOTHING""",
        (conn_id, user_id, type_, state_dir, status),
    )


async def set_connection_status(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    type_: str,
    status: str,
    display: str | None = None,
) -> None:
    if display is not None:
        await db.execute(
            """UPDATE provider_connections
               SET status = ?, display = ?, updated_at = datetime('now')
               WHERE user_id = ? AND type = ?""",
            (status, display, user_id, type_),
        )
    else:
        await db.execute(
            """UPDATE provider_connections
               SET status = ?, updated_at = datetime('now')
               WHERE user_id = ? AND type = ?""",
            (status, user_id, type_),
        )


async def list_linked_connections(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """For the supervisor: every WhatsApp account currently linked."""
    cur = await db.execute(
        "SELECT * FROM provider_connections WHERE type = 'whatsapp' AND status = 'linked'"
    )
    return list(await cur.fetchall())


# --- feeds -------------------------------------------------------------------


async def list_feeds(db: aiosqlite.Connection, user_id: str) -> list[aiosqlite.Row]:
    cur = await db.execute(
        "SELECT * FROM feeds WHERE user_id = ? ORDER BY created_at", (user_id,)
    )
    return list(await cur.fetchall())


async def get_feed(db: aiosqlite.Connection, feed_id: str) -> aiosqlite.Row | None:
    cur = await db.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,))
    return await cur.fetchone()


async def insert_feed(
    db: aiosqlite.Connection,
    *,
    feed_id: str,
    user_id: str,
    name: str,
    connection_id: str,
    tg_bot_token: str = "",
    tg_chat_id: str = "",
) -> None:
    await db.execute(
        """INSERT INTO feeds (id, user_id, name, connection_id, tg_bot_token, tg_chat_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (feed_id, user_id, name, connection_id, tg_bot_token, tg_chat_id),
    )


async def update_feed(
    db: aiosqlite.Connection,
    *,
    feed_id: str,
    name: str,
    tg_bot_token: str,
    tg_chat_id: str,
) -> None:
    await db.execute(
        "UPDATE feeds SET name = ?, tg_bot_token = ?, tg_chat_id = ? WHERE id = ?",
        (name, tg_bot_token, tg_chat_id, feed_id),
    )


async def delete_feed(db: aiosqlite.Connection, feed_id: str) -> None:
    await db.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))


async def list_feed_sources(db: aiosqlite.Connection, feed_id: str) -> list[aiosqlite.Row]:
    cur = await db.execute(
        "SELECT * FROM feed_sources WHERE feed_id = ? ORDER BY local_name, chat_jid",
        (feed_id,),
    )
    return list(await cur.fetchall())


async def replace_feed_sources(
    db: aiosqlite.Connection,
    *,
    feed_id: str,
    sources: list[tuple[str, str]],
    new_id,
) -> None:
    """Replace a feed's source set wholesale. `sources` = [(chat_jid, local_name)]."""
    await db.execute("DELETE FROM feed_sources WHERE feed_id = ?", (feed_id,))
    for chat_jid, local_name in sources:
        await db.execute(
            "INSERT INTO feed_sources (id, feed_id, chat_jid, local_name) VALUES (?, ?, ?, ?)",
            (new_id(), feed_id, chat_jid, local_name or None),
        )
