"""Feeds: a set of source chats pointing at one Telegram destination.

The source picker reads the chats of one link (`wacli chats list` with
HOME=state_dir). That read is readonly and coexists happily with the
background sync — WAL means readers never block the writer.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from newsroom_shared import db as dbm
from newsroom_shared import settings_store
from newsroom_shared.ids import new_row_id
from newsroom_shared.providers import get_provider
from newsroom_shared.telegram import TelegramClient, escape_html
from newsroom_shared.wacli_client import WacliError

from ..deps import render, require_user
from ..settings import user_state_dir

router = APIRouter()


async def _owned_feed(db, feed_id: str, user_id: str):
    feed = await dbm.get_feed(db, feed_id)
    if feed is None or feed["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="feed not found")
    return feed


@router.get("/feeds")
async def feeds_list(request: Request, user=Depends(require_user)):
    async with dbm.connect() as db:
        feeds = await dbm.list_feeds(db, user["id"])
        view = []
        for f in feeds:
            srcs = await dbm.list_feed_sources(db, f["id"])
            view.append({"feed": f, "source_count": len(srcs)})
    return await render(request, "feeds.html", user=user, feeds=view)


@router.get("/feeds/new")
async def feed_new_form(request: Request, user=Depends(require_user)):
    return await render(request, "feed_new.html", user=user, error=None)


@router.post("/feeds")
async def feed_create(
    request: Request,
    user=Depends(require_user),
    name: str = Form(...),
    tg_bot_token: str = Form(""),
    tg_chat_id: str = Form(""),
):
    name = name.strip()
    if not name:
        return await render(request, "feed_new.html", user=user, error="Name can't be empty", status_code=400)

    async with dbm.connect() as db:
        conn = await dbm.get_connection(db, user["id"], "whatsapp")
        if conn is None:
            await dbm.upsert_connection(
                db, conn_id=new_row_id(), user_id=user["id"],
                type_="whatsapp", state_dir=user_state_dir(user["id"]),
            )
            conn = await dbm.get_connection(db, user["id"], "whatsapp")
        feed_id = new_row_id()
        await dbm.insert_feed(
            db, feed_id=feed_id, user_id=user["id"], name=name,
            connection_id=conn["id"], tg_bot_token=tg_bot_token.strip(),
            tg_chat_id=tg_chat_id.strip(),
        )
        await db.commit()
    return RedirectResponse(f"/feeds/{feed_id}", status_code=303)


@router.get("/feeds/{feed_id}")
async def feed_edit_form(request: Request, feed_id: str, user=Depends(require_user)):
    async with dbm.connect() as db:
        feed = await _owned_feed(db, feed_id, user["id"])
        conn = await dbm.get_connection_by_id(db, feed["connection_id"])
        selected = {s["chat_jid"]: (s["local_name"] or "") for s in await dbm.list_feed_sources(db, feed_id)}

    chats: list = []
    chats_error = None
    if conn and conn["status"] == "linked":
        try:
            provider = get_provider(conn["type"])
            all_chats = await provider.list_chats(conn["state_dir"])
            # Groups first, then by name.
            chats = sorted(all_chats, key=lambda c: (c.kind != "group", (c.name or "").lower()))
        except WacliError as exc:
            chats_error = str(exc)
    else:
        chats_error = "WhatsApp not linked — connect your account first."

    return await render(
        request, "feed_edit.html", user=user, feed=feed, conn=conn,
        chats=chats, selected=selected, chats_error=chats_error,
    )


@router.post("/feeds/{feed_id}")
async def feed_save(request: Request, feed_id: str, user=Depends(require_user)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    tg_bot_token = (form.get("tg_bot_token") or "").strip()
    tg_chat_id = (form.get("tg_chat_id") or "").strip()

    async with dbm.connect() as db:
        await _owned_feed(db, feed_id, user["id"])
        await dbm.update_feed(
            db, feed_id=feed_id, name=name or "Feed",
            tg_bot_token=tg_bot_token, tg_chat_id=tg_chat_id,
        )
        # Only touch sources when the picker actually rendered (sources_present).
        # Otherwise saving settings while wacli is unreachable would wipe them.
        if form.get("sources_present"):
            sources = [
                (jid, (form.get(f"name__{jid}") or "").strip())
                for jid in form.getlist("source")
            ]
            await dbm.replace_feed_sources(
                db, feed_id=feed_id, sources=sources, new_id=new_row_id
            )
        await db.commit()
    return RedirectResponse(f"/feeds/{feed_id}", status_code=303)


@router.post("/feeds/{feed_id}/delete")
async def feed_delete(request: Request, feed_id: str, user=Depends(require_user)):
    async with dbm.connect() as db:
        await _owned_feed(db, feed_id, user["id"])
        await dbm.delete_feed(db, feed_id)
        await db.commit()
    return RedirectResponse("/feeds", status_code=303)


@router.post("/feeds/{feed_id}/test")
async def feed_test(request: Request, feed_id: str, user=Depends(require_user)):
    async with dbm.connect() as db:
        feed = await _owned_feed(db, feed_id, user["id"])

    ok = True
    detail = "Test message sent."
    if not feed["tg_bot_token"] or not feed["tg_chat_id"]:
        ok, detail = False, "Set a Telegram bot token and chat id first."
    else:
        site_name = await settings_store.get("site_name")
        try:
            async with TelegramClient(feed["tg_bot_token"]) as tg:
                await tg.send_html(
                    feed["tg_chat_id"],
                    f"<b>{escape_html(feed['name'])}</b>\n"
                    f"{escape_html(site_name)} — connection test.",
                )
        except httpx.HTTPStatusError as exc:
            ok, detail = False, f"Telegram rejected the request: {exc.response.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"Could not send: {exc}"

    async with dbm.connect() as db:
        feed = await _owned_feed(db, feed_id, user["id"])
        conn = await dbm.get_connection_by_id(db, feed["connection_id"])
        selected = {s["chat_jid"]: (s["local_name"] or "") for s in await dbm.list_feed_sources(db, feed_id)}
    chats: list = []
    chats_error = None
    if conn and conn["status"] == "linked":
        try:
            provider = get_provider(conn["type"])
            chats = sorted(await provider.list_chats(conn["state_dir"]),
                           key=lambda c: (c.kind != "group", (c.name or "").lower()))
        except WacliError as exc:
            chats_error = str(exc)
    return await render(
        request, "feed_edit.html", user=user, feed=feed, conn=conn,
        chats=chats, selected=selected, chats_error=chats_error,
        test_ok=ok, test_detail=detail,
    )
