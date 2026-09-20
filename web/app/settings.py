"""Container paths plus helpers for reading runtime settings.

Only two values come from the environment, and both describe where the image
keeps its data rather than anything an operator would want to tune. Every real
setting lives in app.db (`newsroom_shared.settings_store`) and is edited from
the dashboard.
"""

from __future__ import annotations

import os

from newsroom_shared import settings_store

APP_DB = os.environ.get("APP_DB", "/wacli-state/app.db")
STATE_ROOT = os.environ.get("WACLI_STATE_ROOT", "/wacli-state")

SESSION_TTL_SEC = 30 * 86400
COOKIE_NAME = "newsroom_session"


def user_state_dir(user_id: str) -> str:
    return f"{STATE_ROOT}/users/{user_id}"


def scope_base_url(scope) -> str:
    """External origin of the current request, e.g. `https://news.example.com`.

    Uvicorn runs with `--proxy-headers`, so `scope["scheme"]` already reflects
    `X-Forwarded-Proto`; the Host header is passed through unchanged by both
    Cloudflare Tunnel and the usual reverse proxies.
    """
    host = ""
    for name, value in scope.get("headers", []):
        if name == b"host":
            host = value.decode("latin-1")
            break
    if not host:
        server = scope.get("server") or ("localhost", None)
        host = f"{server[0]}:{server[1]}" if server[1] else str(server[0])
    return f"{scope.get('scheme', 'http')}://{host}"


async def base_url(scope) -> str:
    """The configured public URL, or the request's own origin if unset.

    This is the OAuth issuer and the connector URL, so a deployment should pin
    it on `/settings`; the fallback only keeps a half-configured install usable.
    """
    configured = (await settings_store.get("public_base_url")).rstrip("/")
    return configured or scope_base_url(scope)


def cookie_secure(scope) -> bool:
    """Session cookies get `Secure` exactly when the browser came in over https.

    Derived from the connection rather than from the configured public URL: a
    `Secure` cookie handed back over plain http is simply dropped by the
    browser, so guessing from configuration would lock people out of a local
    or pre-TLS install. Behind a proxy this needs `X-Forwarded-Proto`, which
    uvicorn reads for us — see the Dockerfile.
    """
    return scope.get("scheme") == "https"
