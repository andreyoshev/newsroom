"""Runtime configuration, kept in app.db instead of environment variables.

Everything an operator can change — public URL, signup policy, secrets, sync
limits — lives in the `app_settings` table and is edited from the dashboard:
once in the first-run wizard, afterwards on `/settings`. The environment only
carries paths the container owns (`APP_DB`, `WACLI_STATE_ROOT`), because those
describe the image layout rather than a preference.

Reads are cached for a few seconds. Settings are consulted on nearly every
request (base URL, signup policy) but change once in a blue moon; writes drop
the cache immediately, so the dashboard never shows a stale value.
"""

from __future__ import annotations

import secrets
import time

import aiosqlite

from . import db as dbm

SIGNUP_OPEN = "open"
SIGNUP_INVITE = "invite"
SIGNUP_CLOSED = "closed"
SIGNUP_MODES = (SIGNUP_OPEN, SIGNUP_INVITE, SIGNUP_CLOSED)

#: Every known setting with the value a fresh install starts from. Keys missing
#: from the table fall back here, so adding a setting never needs a migration.
DEFAULTS: dict[str, str] = {
    # Shown as the brand in the dashboard and in Telegram test messages.
    "site_name": "newsroom",
    # External https address. Doubles as the OAuth issuer and as the connector
    # URL handed to Claude, so it must match what the tunnel/proxy serves.
    # Empty = derive it from the incoming request (works behind a proxy that
    # forwards Host and X-Forwarded-Proto).
    "public_base_url": "",
    # open = anyone can register, invite = invite code required, closed = no signup.
    "signup_mode": SIGNUP_INVITE,
    "invite_code": "",
    # Secret for the deep health endpoints (/health/integrations, /health/whatsapp).
    # Empty = those endpoints are public.
    "health_token": "",
    # How often the supervisor reconciles running syncs against the database.
    "supervisor_poll_sec": "5",
    # Passed to each `wacli sync --follow` child to cap store growth.
    "wacli_sync_max_messages": "100000",
    "wacli_sync_max_db_size": "500MB",
    # Flipped to "1" once the first-run wizard has been completed.
    "setup_complete": "0",
}

_CACHE_TTL_SEC = 3.0

_cache: dict[str, str] | None = None
_cached_at = 0.0


def invalidate() -> None:
    """Drop the cache so the next read hits the database."""
    global _cache
    _cache = None


async def read_all(db: aiosqlite.Connection) -> dict[str, str]:
    """All settings on an open connection, defaults filled in."""
    cur = await db.execute("SELECT key, value FROM app_settings")
    stored = {row["key"]: row["value"] for row in await cur.fetchall()}
    return {**DEFAULTS, **stored}


async def load(*, force: bool = False) -> dict[str, str]:
    """All settings, served from a short-lived process cache."""
    global _cache, _cached_at
    now = time.monotonic()
    if not force and _cache is not None and now - _cached_at < _CACHE_TTL_SEC:
        return _cache
    async with dbm.connect() as db:
        values = await read_all(db)
    _cache, _cached_at = values, now
    return values


async def get(key: str) -> str:
    return (await load()).get(key, DEFAULTS.get(key, ""))


async def write(db: aiosqlite.Connection, values: dict[str, str]) -> None:
    """Upsert settings into an open transaction.

    Finish with `commit()` below rather than `db.commit()`. The cache is read
    through a separate connection, so dropping it before the transaction lands
    would just re-cache the old values for another few seconds.
    """
    for key, value in values.items():
        await db.execute(
            """INSERT INTO app_settings (key, value, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE
                 SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, value),
        )


async def commit(db: aiosqlite.Connection) -> None:
    """Commit a settings transaction and drop the cache, in that order."""
    await db.commit()
    invalidate()


async def bootstrap(db: aiosqlite.Connection) -> None:
    """Seed missing keys and reconcile an install that predates this table.

    Never overwrites a value that is already there. Runs on every start, right
    after the schema is created, so it is safe to call repeatedly.
    """
    for key, value in DEFAULTS.items():
        await db.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (key, value)
        )

    # A database that already holds accounts was set up before the wizard
    # existed: skip it, and make sure somebody can still reach /settings.
    cur = await db.execute("SELECT id FROM users ORDER BY created_at, id LIMIT 1")
    first_user = await cur.fetchone()
    if first_user is None:
        return

    await db.execute("UPDATE app_settings SET value = '1' WHERE key = 'setup_complete'")
    cur = await db.execute("SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1")
    if await cur.fetchone() is None:
        await db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (first_user["id"],))


def new_invite_code() -> str:
    """Short enough to paste into a chat, long enough not to be guessed."""
    return secrets.token_urlsafe(9)


def new_health_token() -> str:
    return secrets.token_hex(24)
