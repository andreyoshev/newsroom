"""Identifier and token generation.

`user_id` ends up in filesystem paths (`/wacli-state/users/<id>`), so it is
short, unguessable and safe in both URLs and paths: `token_hex` gives [0-9a-f].
"""

from __future__ import annotations

import secrets


def new_user_id() -> str:
    return secrets.token_hex(8)


def new_row_id() -> str:
    return secrets.token_hex(16)


def new_secret_token() -> str:
    """Sessions, OAuth access/refresh tokens, auth codes: a long url-safe secret."""
    return secrets.token_urlsafe(32)
