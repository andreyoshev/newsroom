"""Health endpoints, shaped for an uptime monitor such as Uptime Kuma.

/health                          liveness (public): the process is up and app.db opens.
/health/integrations             deep check, aggregated over every linked account.
/health/whatsapp                 the same, for the single "whatsapp" integration.
/health/whatsapp/<connection_id> one specific link, by its id.
    Owners find that id on /connections as "Monitor ID". The service is
    multi-user, so everyone monitors their own account with their own id. The
    id is not a secret — the deep endpoints sit behind the health token from
    /settings (a wrong token gets 404, not 403).

"Is the token still alive" comes from `wacli doctor`, which answers even while
a live `sync` holds the store lock:
    authenticated      the session is intact (not logged out) -> the main signal;
    linked_jid         which number is linked;
    lock_held          a live sync holds the store lock (syncing is running);
    store.last_sync_at when it last synced.
WhatsApp logged the device out -> authenticated=false -> 503. Sync or the
supervisor died -> lock_held=false -> 503.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from newsroom_shared import db as dbm
from newsroom_shared import settings_store
from newsroom_shared import wacli_client

router = APIRouter()


async def _guard(request: Request) -> None:
    token = await settings_store.get("health_token")
    if not token:
        return
    provided = (
        request.headers.get("x-health-token")
        or request.query_params.get("token")
        or ""
    )
    if not secrets.compare_digest(provided, token):
        raise HTTPException(status_code=404, detail="Not Found")


async def _db_ok() -> None:
    async with dbm.connect() as db:
        await db.execute("SELECT 1")


async def _probe_connection(row) -> dict:
    """One WhatsApp link, as reported by `wacli doctor`."""
    doctor = await wacli_client.doctor(row["state_dir"])
    d = doctor or {}
    available = doctor is not None                                           # wacli ran
    authorized = bool(d.get("authenticated")) and bool(d.get("linked_jid"))  # token alive
    working = bool(d.get("lock_held"))                                       # sync holds the store
    ok = available and authorized and working
    last_sync = (d.get("store") or {}).get("last_sync_at")
    return {
        "account": d.get("linked_jid") or row["display"] or row["user_id"],
        "connection_id": row["id"],
        "available": available, "authorized": authorized, "working": working,
        "detail": (f"authenticated={d.get('authenticated')} jid={d.get('linked_jid')} "
                   f"lock_held={d.get('lock_held')} state={d.get('connection_state')} "
                   f"last_sync_at={last_sync}"),
        "ok": ok,
    }


def _aggregate(name: str, accounts: list[dict], empty_detail: str) -> dict:
    if not accounts:
        return {"name": name, "available": False, "authorized": False, "working": False,
                "token_age_s": None, "detail": empty_detail, "ok": False, "accounts": []}
    return {"name": name,
            "available": all(a["available"] for a in accounts),
            "authorized": all(a["authorized"] for a in accounts),
            "working": all(a["working"] for a in accounts),
            "token_age_s": None,
            "detail": "; ".join(f"{a['account']}: {a['detail']}" for a in accounts),
            "ok": all(a["ok"] for a in accounts),
            "accounts": accounts}


async def probe_whatsapp() -> dict:
    """Aggregate over every linked WhatsApp account."""
    async with dbm.connect() as db:
        rows = await dbm.list_linked_connections(db)
    accounts = [await _probe_connection(r) for r in rows]
    return _aggregate("whatsapp", accounts, "no linked whatsapp connections")


async def probe_whatsapp_one(conn_id: str) -> dict | None:
    """One link by id (None when the id is unknown or not a WhatsApp link)."""
    async with dbm.connect() as db:
        row = await dbm.get_connection_by_id(db, conn_id)
    if row is None or row["type"] != "whatsapp":
        return None
    return _aggregate("whatsapp", [await _probe_connection(row)], "")


PROBES = {"whatsapp": probe_whatsapp}


def _result(c: dict) -> JSONResponse:
    return JSONResponse(
        status_code=200 if c["ok"] else 503,
        content={"status": "ok" if c["ok"] else "degraded", "ok": c["ok"],
                 "checked": 1, "integrations": [c]},
    )


@router.get("/health")
async def health_live():
    """Liveness: the process is up and the database opens."""
    try:
        await _db_ok()
        return {"status": "ok"}
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=503,
                            content={"status": "down", "detail": str(e)})


@router.get("/health/integrations")
async def health_integrations(request: Request):
    await _guard(request)
    checks = [await probe() for probe in PROBES.values()]
    ok = all(c["ok"] for c in checks) if checks else True
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "degraded", "ok": ok,
                 "checked": len(checks), "integrations": checks},
    )


@router.get("/health/whatsapp/{conn_id}")
async def health_whatsapp_one(conn_id: str, request: Request):
    await _guard(request)
    c = await probe_whatsapp_one(conn_id)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Unknown: {conn_id}")
    return _result(c)


@router.get("/health/{name}")
async def health_one(name: str, request: Request):
    await _guard(request)
    if name not in PROBES:
        raise HTTPException(status_code=404, detail=f"Unknown: {name}")
    return _result(await PROBES[name]())
