"""Admin settings: everything that used to be an environment variable.

Reachable only by an admin account (the first one created by the wizard).
Changes take effect on the next request — nothing here needs a restart, with
one exception called out in the UI: changing the public URL invalidates the
OAuth issuer, so existing Claude connectors have to be re-authorized.
"""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, Form, Request

from newsroom_shared import db as dbm
from newsroom_shared import settings_store

from ..deps import render, require_admin
from ..settings import base_url, scope_base_url, user_state_dir

router = APIRouter()


async def _page(request: Request, user, *, notice: str | None = None,
                error: str | None = None, status_code: int = 200):
    conf = await settings_store.load(force=True)
    async with dbm.connect() as db:
        users = await dbm.list_users(db)
    return await render(
        request, "settings.html", user=user, conf=conf, users=users,
        notice=notice, error=error, status_code=status_code,
        effective_base_url=await base_url(request.scope),
        request_base_url=scope_base_url(request.scope),
        signup_modes=settings_store.SIGNUP_MODES,
    )


@router.get("/settings")
async def settings_page(request: Request, user=Depends(require_admin)):
    return await _page(request, user)


@router.post("/settings")
async def settings_save(
    request: Request,
    user=Depends(require_admin),
    site_name: str = Form("newsroom"),
    public_base_url: str = Form(""),
    signup_mode: str = Form(settings_store.SIGNUP_INVITE),
    invite_code: str = Form(""),
    health_token: str = Form(""),
    supervisor_poll_sec: str = Form("5"),
    wacli_sync_max_messages: str = Form("100000"),
    wacli_sync_max_db_size: str = Form("500MB"),
    regenerate: str = Form(""),
):
    if regenerate == "invite":
        invite_code = settings_store.new_invite_code()
    elif regenerate == "health":
        health_token = settings_store.new_health_token()

    base = public_base_url.strip().rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    if signup_mode not in settings_store.SIGNUP_MODES:
        signup_mode = settings_store.SIGNUP_INVITE
    if signup_mode == settings_store.SIGNUP_INVITE and not invite_code.strip():
        invite_code = settings_store.new_invite_code()

    try:
        poll = max(1, min(300, int(supervisor_poll_sec)))
    except ValueError:
        return await _page(request, user, error="Poll interval must be a number of seconds",
                           status_code=400)
    try:
        max_messages = max(1000, int(wacli_sync_max_messages))
    except ValueError:
        return await _page(request, user, error="Message cap must be a number",
                           status_code=400)

    async with dbm.connect() as db:
        await settings_store.write(db, {
            "site_name": site_name.strip() or "newsroom",
            "public_base_url": base,
            "signup_mode": signup_mode,
            "invite_code": invite_code.strip(),
            "health_token": health_token.strip(),
            "supervisor_poll_sec": str(poll),
            "wacli_sync_max_messages": str(max_messages),
            "wacli_sync_max_db_size": wacli_sync_max_db_size.strip() or "500MB",
        })
        await settings_store.commit(db)

    notice = {
        "invite": "New invite code generated.",
        "health": "New health token generated.",
    }.get(regenerate, "Settings saved.")
    return await _page(request, user, notice=notice)


@router.post("/settings/users/{user_id}/delete")
async def delete_user(request: Request, user_id: str, user=Depends(require_admin)):
    if user_id == user["id"]:
        return await _page(request, user, error="You can't delete your own account.",
                           status_code=400)

    async with dbm.connect() as db:
        target = await dbm.get_user_by_id(db, user_id)
        if target is None:
            return await _page(request, user, error="No such account.", status_code=404)
        await dbm.delete_user(db, user_id)
        await db.commit()

    # Rows are gone, so the supervisor stops that account's sync on its next
    # pass; drop the WhatsApp store too rather than leave messages on disk.
    shutil.rmtree(user_state_dir(user_id), ignore_errors=True)
    return await _page(request, user, notice=f"Deleted {target['email']}.")


@router.post("/settings/users/{user_id}/admin")
async def toggle_admin(
    request: Request, user_id: str, user=Depends(require_admin), make: str = Form("1")
):
    if user_id == user["id"]:
        return await _page(request, user, error="You can't change your own role.",
                           status_code=400)
    async with dbm.connect() as db:
        target = await dbm.get_user_by_id(db, user_id)
        if target is None:
            return await _page(request, user, error="No such account.", status_code=404)
        await dbm.set_admin(db, user_id, make == "1")
        await db.commit()
    verb = "now an admin" if make == "1" else "no longer an admin"
    return await _page(request, user, notice=f"{target['email']} is {verb}.")
