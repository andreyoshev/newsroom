"""First-run wizard: the one screen that turns a fresh container into a service.

It runs only while the database has no accounts. Everything it collects —
admin credentials, the public URL, who may sign up — is written to
`app_settings` and can be changed later on `/settings`; nothing here ever
touches the environment.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from newsroom_shared import db as dbm
from newsroom_shared import settings_store
from newsroom_shared.ids import new_row_id, new_user_id
from newsroom_shared.security import hash_password

from ..deps import render
from ..settings import scope_base_url, user_state_dir
from .auth import start_session

router = APIRouter()

#: Paths that stay reachable while setup is pending: the wizard itself, its
#: styling, and the liveness probe an orchestrator may already be polling.
EXEMPT_PREFIXES = ("/setup", "/static/", "/healthz", "/health")


async def is_complete() -> bool:
    return (await settings_store.get("setup_complete")) == "1"


def _normalize_base_url(raw: str, fallback: str) -> str:
    url = raw.strip().rstrip("/")
    if not url:
        return fallback
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url


@router.get("/setup")
async def setup_form(request: Request):
    if await is_complete():
        return RedirectResponse("/", status_code=303)
    return await render(
        request, "setup.html",
        error=None,
        suggested_base_url=scope_base_url(request.scope),
        suggested_invite=settings_store.new_invite_code(),
        values={},
    )


@router.post("/setup")
async def setup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    site_name: str = Form("newsroom"),
    public_base_url: str = Form(""),
    signup_mode: str = Form(settings_store.SIGNUP_INVITE),
    invite_code: str = Form(""),
):
    if await is_complete():
        return RedirectResponse("/", status_code=303)

    email = email.strip().lower()
    site_name = site_name.strip() or "newsroom"
    base = _normalize_base_url(public_base_url, scope_base_url(request.scope))
    if signup_mode not in settings_store.SIGNUP_MODES:
        signup_mode = settings_store.SIGNUP_INVITE
    invite_code = invite_code.strip()

    async def fail(msg: str):
        return await render(
            request, "setup.html", error=msg, status_code=400,
            suggested_base_url=scope_base_url(request.scope),
            suggested_invite=invite_code or settings_store.new_invite_code(),
            values={
                "email": email, "site_name": site_name,
                "public_base_url": public_base_url, "signup_mode": signup_mode,
            },
        )

    if "@" not in email:
        return await fail("That doesn't look like an email")
    if len(password) < 8:
        return await fail("Password must be at least 8 characters")
    if signup_mode == settings_store.SIGNUP_INVITE and not invite_code:
        return await fail("Invite-only signup needs an invite code")

    async with dbm.connect() as db:
        # Re-check inside the transaction: the wizard is unauthenticated, and
        # two people hitting it at once must not both become admin.
        if await dbm.count_users(db) > 0:
            return RedirectResponse("/", status_code=303)

        user_id = new_user_id()
        pwhash = await run_in_threadpool(hash_password, password)
        await dbm.insert_user(
            db, user_id=user_id, email=email, password_hash=pwhash, is_admin=True
        )
        await dbm.upsert_connection(
            db, conn_id=new_row_id(), user_id=user_id,
            type_="whatsapp", state_dir=user_state_dir(user_id), status="unlinked",
        )
        await settings_store.write(db, {
            "site_name": site_name,
            "public_base_url": base,
            "signup_mode": signup_mode,
            "invite_code": invite_code if signup_mode == settings_store.SIGNUP_INVITE else "",
            "health_token": settings_store.new_health_token(),
            "setup_complete": "1",
        })
        resp = RedirectResponse("/", status_code=303)
        await start_session(request, resp, db, user_id)
        await settings_store.commit(db)
    return resp
