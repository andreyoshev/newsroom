"""Sign up, sign in, sign out. Sessions are server side, the token in a cookie."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response

from newsroom_shared import db as dbm
from newsroom_shared import settings_store
from newsroom_shared.ids import new_row_id, new_secret_token, new_user_id
from newsroom_shared.security import hash_password, verify_password

from ..deps import load_user, render
from ..settings import COOKIE_NAME, SESSION_TTL_SEC, cookie_secure, user_state_dir

router = APIRouter()


def safe_next(next_url: str) -> str:
    """Internal paths only — guards against open redirects.

    Protocol-relative `//host` and `/\\host` also start with a slash and the
    browser reads them as another origin, so they are rejected too.
    """
    if next_url.startswith("/") and not next_url.startswith(("//", "/\\")):
        return next_url
    return "/"


async def start_session(request: Request, resp: Response, db, user_id: str) -> None:
    """Create a session row and attach its cookie to `resp`."""
    token = new_secret_token()
    await dbm.insert_session(
        db, token=token, user_id=user_id, expires_at=dbm.iso_in(SESSION_TTL_SEC)
    )
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SEC,
        httponly=True,
        secure=cookie_secure(request.scope),
        samesite="lax",
        path="/",
    )


@router.get("/login")
async def login_form(request: Request, next: str = "/"):
    if await load_user(request):
        return RedirectResponse("/", status_code=303)
    return await render(request, "login.html", next=next, error=None)


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    async with dbm.connect() as db:
        user = await dbm.get_user_by_email(db, email)
        # argon2 is deliberately expensive; run it off the event loop.
        pw_ok = (
            await run_in_threadpool(verify_password, user["password_hash"], password)
            if user is not None else False
        )
        if not pw_ok:
            return await render(
                request, "login.html", next=next,
                error="Wrong email or password", status_code=400,
            )
        resp = RedirectResponse(safe_next(next), status_code=303)
        await start_session(request, resp, db, user["id"])
        await db.commit()
    return resp


@router.get("/signup")
async def signup_form(request: Request):
    if await load_user(request):
        return RedirectResponse("/", status_code=303)
    mode = await settings_store.get("signup_mode")
    if mode == settings_store.SIGNUP_CLOSED:
        return await render(request, "signup_closed.html", status_code=403)
    return await render(
        request, "signup.html", error=None,
        invite_required=mode == settings_store.SIGNUP_INVITE,
    )


@router.post("/signup")
async def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    invite: str = Form(""),
):
    conf = await settings_store.load()
    mode = conf["signup_mode"]
    if mode == settings_store.SIGNUP_CLOSED:
        return await render(request, "signup_closed.html", status_code=403)

    async def fail(msg: str):
        return await render(
            request, "signup.html", error=msg,
            invite_required=mode == settings_store.SIGNUP_INVITE, status_code=400,
        )

    if mode == settings_store.SIGNUP_INVITE and invite.strip() != conf["invite_code"]:
        return await fail("Invalid invite code")
    email = email.strip().lower()
    if "@" not in email:
        return await fail("That doesn't look like an email")
    if len(password) < 8:
        return await fail("Password must be at least 8 characters")

    async with dbm.connect() as db:
        if await dbm.get_user_by_email(db, email):
            return await fail("This email is already registered")

        user_id = new_user_id()
        pwhash = await run_in_threadpool(hash_password, password)
        await dbm.insert_user(db, user_id=user_id, email=email, password_hash=pwhash)
        # Create the WhatsApp connection straight away (unlinked) — its
        # state_dir is fixed by the user id.
        await dbm.upsert_connection(
            db, conn_id=new_row_id(), user_id=user_id,
            type_="whatsapp", state_dir=user_state_dir(user_id), status="unlinked",
        )
        resp = RedirectResponse("/", status_code=303)
        await start_session(request, resp, db, user_id)
        await db.commit()
    return resp


@router.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        async with dbm.connect() as db:
            await dbm.delete_session(db, token)
            await db.commit()
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
