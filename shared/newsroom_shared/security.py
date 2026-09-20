"""Password hashing (argon2) and constant-time token comparison."""

from __future__ import annotations

import hmac

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001 — a corrupt hash must not break login
        return False


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
