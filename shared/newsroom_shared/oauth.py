"""A minimal OAuth 2.1 authorization server (public client plus PKCE).

Hand-written rather than authlib because the flow is specific: dynamic client
registration from Claude, a PKCE-only public client, and a consent screen that
binds the grant to one feed. That is a hundred and fifty lines of plain code
over app.db instead of a framework to saddle. The HTTP handlers and the
discovery metadata live in the web service.

A grant is scoped to a single feed: the token carries (user_id, feed_id),
which maps one-to-one onto "one routine, one Telegram destination".
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

import aiosqlite

from . import db as dbm
from .ids import new_row_id, new_secret_token

CODE_TTL_SEC = 600           # 10 minutes to exchange the code
ACCESS_TTL_SEC = 3600        # 1 hour
REFRESH_TTL_SEC = 90 * 86400 # 90 days, so a scheduled routine never needs a re-login


@dataclass
class Tenant:
    user_id: str
    feed_id: str | None


# --- PKCE --------------------------------------------------------------------


def verify_pkce_s256(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return expected == challenge


# --- dynamic client registration --------------------------------------------


async def register_client(
    db: aiosqlite.Connection, *, client_name: str, redirect_uris_json: str
) -> str:
    client_id = new_row_id()
    await db.execute(
        "INSERT INTO oauth_clients (client_id, client_name, redirect_uris) VALUES (?, ?, ?)",
        (client_id, client_name, redirect_uris_json),
    )
    await db.commit()
    return client_id


async def get_client(db: aiosqlite.Connection, client_id: str) -> aiosqlite.Row | None:
    cur = await db.execute(
        "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
    )
    return await cur.fetchone()


# --- authorization code ------------------------------------------------------


async def issue_code(
    db: aiosqlite.Connection,
    *,
    client_id: str,
    user_id: str,
    feed_id: str | None,
    redirect_uri: str,
    code_challenge: str,
) -> str:
    code = new_secret_token()
    await db.execute(
        """INSERT INTO oauth_authorization_codes
           (code, client_id, user_id, feed_id, redirect_uri, code_challenge, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (code, client_id, user_id, feed_id, redirect_uri, code_challenge, dbm.iso_in(CODE_TTL_SEC)),
    )
    await db.commit()
    return code


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "") -> None:
        super().__init__(description or error)
        self.error = error
        self.description = description


async def exchange_code(
    db: aiosqlite.Connection,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, object]:
    cur = await db.execute(
        "SELECT * FROM oauth_authorization_codes WHERE code = ?", (code,)
    )
    row = await cur.fetchone()
    if row is None or row["used"]:
        raise OAuthError("invalid_grant", "code not found or already used")
    if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
        raise OAuthError("invalid_grant", "client/redirect mismatch")
    if dbm.is_expired(row["expires_at"]):
        raise OAuthError("invalid_grant", "code expired")
    if not verify_pkce_s256(code_verifier, row["code_challenge"]):
        raise OAuthError("invalid_grant", "PKCE verification failed")

    await db.execute("UPDATE oauth_authorization_codes SET used = 1 WHERE code = ?", (code,))
    return await _issue_tokens(
        db, client_id=client_id, user_id=row["user_id"], feed_id=row["feed_id"]
    )


# --- tokens ------------------------------------------------------------------


async def _issue_tokens(
    db: aiosqlite.Connection, *, client_id: str, user_id: str, feed_id: str | None
) -> dict[str, object]:
    access = new_secret_token()
    refresh = new_secret_token()
    await db.execute(
        """INSERT INTO oauth_tokens
           (access_token, refresh_token, client_id, user_id, feed_id,
            access_expires_at, refresh_expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            access, refresh, client_id, user_id, feed_id,
            dbm.iso_in(ACCESS_TTL_SEC), dbm.iso_in(REFRESH_TTL_SEC),
        ),
    )
    await db.commit()
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SEC,
    }


async def refresh_tokens(
    db: aiosqlite.Connection, *, refresh_token: str, client_id: str
) -> dict[str, object]:
    cur = await db.execute(
        "SELECT * FROM oauth_tokens WHERE refresh_token = ?", (refresh_token,)
    )
    row = await cur.fetchone()
    if row is None or row["revoked"] or row["client_id"] != client_id:
        raise OAuthError("invalid_grant", "refresh token invalid")
    if dbm.is_expired(row["refresh_expires_at"]):
        raise OAuthError("invalid_grant", "refresh token expired")

    # Rotation: revoke the old pair, hand out a new one.
    await db.execute(
        "UPDATE oauth_tokens SET revoked = 1 WHERE access_token = ?", (row["access_token"],)
    )
    return await _issue_tokens(
        db, client_id=client_id, user_id=row["user_id"], feed_id=row["feed_id"]
    )


async def verify_access_token(db: aiosqlite.Connection, access_token: str) -> Tenant | None:
    cur = await db.execute(
        "SELECT * FROM oauth_tokens WHERE access_token = ?", (access_token,)
    )
    row = await cur.fetchone()
    if row is None or row["revoked"] or dbm.is_expired(row["access_expires_at"]):
        return None
    await db.execute(
        "UPDATE oauth_tokens SET last_used_at = ? WHERE access_token = ?",
        (dbm.iso_now(), access_token),
    )
    await db.commit()
    return Tenant(user_id=row["user_id"], feed_id=row["feed_id"])


async def revoke_for_feed(db: aiosqlite.Connection, feed_id: str) -> None:
    await db.execute("UPDATE oauth_tokens SET revoked = 1 WHERE feed_id = ?", (feed_id,))
