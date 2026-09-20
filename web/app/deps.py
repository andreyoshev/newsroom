"""Sessions, templates and the authentication dependencies."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from newsroom_shared import db as dbm
from newsroom_shared import settings_store

from .settings import COOKIE_NAME, base_url

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


class NeedsLogin(Exception):
    """Raised by require_user; the handler redirects to /login."""

    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


async def load_user(request: Request) -> aiosqlite.Row | None:
    """The current user from the session cookie, or None."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    async with dbm.connect() as db:
        return await dbm.get_session_user(db, token)


async def require_user(request: Request) -> aiosqlite.Row:
    user = await load_user(request)
    if user is None:
        next_url = request.url.path
        if request.url.query:
            next_url += f"?{request.url.query}"
        raise NeedsLogin(next_url)
    return user


async def require_admin(request: Request) -> aiosqlite.Row:
    """Admin-only pages. A non-admin gets 404 rather than 403: the existence of
    the settings page is not something a regular account needs to learn."""
    user = await require_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=404, detail="Not Found")
    return user


async def render(request: Request, name: str, *, status_code: int = 200, **ctx: object):
    """Render a template with the chrome every page needs (brand, base URL)."""
    conf = await settings_store.load()
    ctx.setdefault("base_url", await base_url(request.scope))
    ctx.setdefault("site_name", conf["site_name"] or "newsroom")
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
