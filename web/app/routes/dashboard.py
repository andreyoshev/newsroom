"""The overview page: WhatsApp link status and the user's feeds."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from newsroom_shared import db as dbm

from ..deps import render, require_user

router = APIRouter()


@router.get("/")
async def index(request: Request, user=Depends(require_user)):
    async with dbm.connect() as db:
        conn = await dbm.get_connection(db, user["id"], "whatsapp")
        feeds = await dbm.list_feeds(db, user["id"])
        feed_view = []
        for f in feeds:
            sources = await dbm.list_feed_sources(db, f["id"])
            feed_view.append({"feed": f, "source_count": len(sources)})

    return await render(request, "dashboard.html", user=user, conn=conn, feeds=feed_view)
