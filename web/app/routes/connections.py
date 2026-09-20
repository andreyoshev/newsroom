"""Linking WhatsApp by QR — one isolated store per user.

The QR stream runs `wacli auth` with HOME set to the user's state_dir. The
only coordination with the supervisor happens through the link status:
  pairing  the supervisor stops that account's sync, releasing the store lock;
  linked   the supervisor (re)starts it.
"""

from __future__ import annotations

import asyncio
import os
import re
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, StreamingResponse

from newsroom_shared import db as dbm
from newsroom_shared import wacli_client

from ..deps import render, require_user

router = APIRouter()

ANSI_RE = re.compile(rb"\x1b\[[\d;?]*[a-zA-Z]")
FRAME_START_RE = re.compile(rb"\x1b\[H")
QR_STREAM_TIMEOUT_SEC = 300
# How long we wait for the supervisor to stop the old sync and release the
# store lock. Comfortably longer than its poll interval plus a terminate.
UNLOCK_WAIT_SEC = 25.0


async def _wait_store_unlocked(state_dir: str, timeout_sec: float) -> bool:
    """Wait until no other process (the sync child) holds the store.

    While `sync --follow` is alive, `wacli doctor` reports connection_state =
    'locked_by_other_process'; once the supervisor stops the child the state
    changes and `wacli auth` can run. Returns False on timeout.
    """
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        doctor = await wacli_client.doctor(state_dir)
        if doctor is not None and doctor.get("connection_state") != "locked_by_other_process":
            return True
        await asyncio.sleep(1.0)
    return False


def _auth_env(state_dir: str) -> dict[str, str]:
    """Environment for `wacli auth`: writable (NOT readonly), aimed at the user's store."""
    env = {k: v for k, v in os.environ.items() if k != "WACLI_READONLY"}
    env["HOME"] = state_dir
    env["XDG_STATE_HOME"] = f"{state_dir}/.local/state"
    return env


def _sse(event: str, data: str) -> str:
    parts = [f"event: {event}"]
    for line in (data.splitlines() or [""]):
        parts.append(f"data: {line}")
    return "\n".join(parts) + "\n\n"


def _latest_frame(buffer: bytes) -> bytes:
    matches = list(FRAME_START_RE.finditer(buffer))
    return buffer[matches[-1].end():] if matches else buffer


@router.get("/connections")
async def connections(request: Request, user=Depends(require_user)):
    async with dbm.connect() as db:
        conn = await dbm.get_connection(db, user["id"], "whatsapp")
    return await render(request, "connections.html", user=user, conn=conn)


@router.get("/connections/whatsapp/qr")
async def qr_page(request: Request, user=Depends(require_user)):
    return await render(request, "qr.html", user=user)


@router.post("/connections/whatsapp/unlink")
async def unlink(request: Request, user=Depends(require_user)):
    async with dbm.connect() as db:
        await dbm.set_connection_status(
            db, user_id=user["id"], type_="whatsapp", status="unlinked"
        )
        await db.commit()
    return RedirectResponse("/connections", status_code=303)


@router.get("/connections/whatsapp/qr/stream")
async def qr_stream(request: Request, user=Depends(require_user)):
    async with dbm.connect() as db:
        conn = await dbm.get_connection(db, user["id"], "whatsapp")
    if conn is None:
        return StreamingResponse(iter([_sse("error", "no connection")]), media_type="text/event-stream")

    state_dir = conn["state_dir"]
    prev_status = conn["status"]
    user_id = user["id"]

    async def set_status(status: str) -> None:
        async with dbm.connect() as db:
            await dbm.set_connection_status(
                db, user_id=user_id, type_="whatsapp", status=status
            )
            await db.commit()

    async def event_gen():
        os.makedirs(f"{state_dir}/.local/state", exist_ok=True)
        # pairing: the supervisor stops this account's sync and drops the lock.
        await set_status("pairing")
        # For an already linked account, actually wait for the lock to clear
        # instead of racing a fixed sleep. A fresh link has no sync child and
        # no lock, so there is nothing to wait for.
        if prev_status == "linked":
            yield _sse("info", "releasing previous session…")
            await _wait_store_unlocked(state_dir, UNLOCK_WAIT_SEC)
        else:
            await asyncio.sleep(0.3)

        proc = await asyncio.create_subprocess_exec(
            "wacli", "auth",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_auth_env(state_dir),
        )
        buffer = bytearray()
        last_frame = ""
        last_send = 0.0
        deadline = time.monotonic() + QR_STREAM_TIMEOUT_SEC
        linked = False

        try:
            yield _sse("info", "starting wacli auth")
            while True:
                if time.monotonic() > deadline:
                    yield _sse("info", "timeout")
                    break
                if await request.is_disconnected():
                    break
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=0.2)
                except asyncio.TimeoutError:
                    chunk = b""
                if chunk:
                    buffer.extend(chunk)
                    if len(buffer) > 65536:
                        del buffer[:32768]
                if proc.returncode is not None:
                    linked = proc.returncode == 0
                    yield _sse("done", "ok" if linked else "failed")
                    break
                frame = ANSI_RE.sub(b"", _latest_frame(bytes(buffer))).decode(errors="replace").strip("\n\r")
                now = time.monotonic()
                if frame and frame != last_frame and now - last_send >= 0.25:
                    yield _sse("frame", frame)
                    last_frame = frame
                    last_send = now
                await asyncio.sleep(0.05)
        finally:
            # SIGTERM the process; no need to reap it, tini as PID 1 collects
            # zombies. What matters is that cancelling this generator (the
            # client closed the tab) still resets the status, or the link would
            # be stuck in 'pairing' forever.
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            final = "linked" if (linked or prev_status == "linked") else "unlinked"
            # shield: the status write lands even if we are being cancelled.
            await asyncio.shield(set_status(final))

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
