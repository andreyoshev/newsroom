"""The web application: dashboard, OAuth authorization server, mounted MCP."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from newsroom_shared import db as dbm

from .deps import NeedsLogin
from .mcp_server import TenantAuthASGI, mcp
from .routes import admin, auth, connections, dashboard, feeds, health, oauth, setup

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)

# MCP as an ASGI app; its own path is "/" and we mount it at /mcp.
# stateless_http=True: no long-lived session task, so every request is handled
# on its own and the tenant contextvar set by TenantAuthASGI is the right one
# for that request, with nothing leaking or going stale between them.
mcp_app = mcp.http_app(path="/", stateless_http=True)


class _NormalizeMcpPath:
    """Let `/mcp` (no trailing slash) reach the mount directly.

    Otherwise the Starlette router answers 307 to `/mcp/` BEFORE the guard
    runs, and it builds Location from the proxied scope (http behind a
    tunnel) — so Claude never sees the 401 challenge and OAuth never starts.
    Rewriting the path in the scope avoids both the redirect and the scheme
    change.
    """

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope["path"] = "/mcp/"
            if scope.get("raw_path") == b"/mcp":
                scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await dbm.migrate()
    # FastMCP's lifespan starts the StreamableHTTP session manager — required.
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(title="newsroom", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(_NormalizeMcpPath)


@app.middleware("http")
async def gate_on_setup(request: Request, call_next):
    """Until the first-run wizard has been completed, there is nothing else.

    A fresh container has no accounts and no configuration, so every path but
    the wizard, its stylesheet and the liveness probe redirects to /setup.
    """
    path = request.url.path
    if not path.startswith(setup.EXEMPT_PREFIXES) and not await setup.is_complete():
        return RedirectResponse("/setup", status_code=303)
    return await call_next(request)


@app.exception_handler(NeedsLogin)
async def needs_login_handler(_request: Request, exc: NeedsLogin):
    return RedirectResponse(f"/login?{urlencode({'next': exc.next_url})}", status_code=303)


@app.get("/healthz")
async def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


app.include_router(health.router)
app.include_router(setup.router)
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(connections.router)
app.include_router(feeds.router)
app.include_router(admin.router)
app.include_router(oauth.router)

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# MCP behind the OAuth bearer guard; its 401 points Claude at discovery.
app.mount("/mcp", TenantAuthASGI(mcp_app))
