"""Supervisor for the multi-account WhatsApp sync.

wacli works with ONE store at a time, addressed through HOME/XDG_STATE_HOME.
To keep N linked accounts in sync, this process reads app.db and runs a
dedicated `wacli sync --follow` per linked WhatsApp account, with HOME set to
that user's state_dir. Crashed children come back with exponential backoff.

Coordination with the web service goes through the link status in the
database, with no IPC at all:
  status `linked`             the child should be running;
  status `pairing` or other   stop the child, releasing the store lock so
                              `wacli auth` can re-link the account;
  link missing from the query stop the child.

Its own configuration — poll interval, sync caps — comes from app_settings and
is re-read every pass, so changing it on /settings takes effect within seconds
and never needs a restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass

from newsroom_shared import db as dbm
from newsroom_shared import settings_store

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s supervisor %(message)s"
)
log = logging.getLogger("supervisor")

BACKOFF_BASE = 2.0
BACKOFF_MAX = 300.0
HEALTHY_UPTIME = 60.0  # a child that lived longer than this resets its backoff


@dataclass
class Child:
    proc: asyncio.subprocess.Process
    state_dir: str
    started_at: float


_children: dict[str, Child] = {}
# user_id -> (earliest_retry_monotonic, current_delay)
_backoff: dict[str, tuple[float, float]] = {}
_stopping = False


def _child_env(state_dir: str, conf: dict[str, str]) -> dict[str, str]:
    """Environment for `sync --follow`: writable, since the sync writes to the store."""
    env = {k: v for k, v in os.environ.items() if k != "WACLI_READONLY"}
    env["HOME"] = state_dir
    env["XDG_STATE_HOME"] = f"{state_dir}/.local/state"
    env["WACLI_SYNC_MAX_MESSAGES"] = conf["wacli_sync_max_messages"]
    env["WACLI_SYNC_MAX_DB_SIZE"] = conf["wacli_sync_max_db_size"]
    return env


async def _spawn(user_id: str, state_dir: str, conf: dict[str, str]) -> None:
    os.makedirs(f"{state_dir}/.local/state", exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "wacli", "sync", "--follow", env=_child_env(state_dir, conf)
    )
    _children[user_id] = Child(proc=proc, state_dir=state_dir, started_at=time.monotonic())
    log.info("spawned sync for user=%s pid=%s dir=%s", user_id, proc.pid, state_dir)


async def _stop(user_id: str, *, reason: str) -> None:
    child = _children.pop(user_id, None)
    if child is None:
        return
    proc = child.proc
    if proc.returncode is None:
        log.info("stopping sync for user=%s (%s)", user_id, reason)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


def _schedule_backoff(user_id: str) -> None:
    prev = _backoff.get(user_id, (0.0, BACKOFF_BASE / 2))
    delay = min(prev[1] * 2, BACKOFF_MAX)
    _backoff[user_id] = (time.monotonic() + delay, delay)
    log.warning("user=%s will retry in %.0fs", user_id, delay)


async def _desired() -> dict[str, str]:
    async with dbm.connect() as db:
        rows = await dbm.list_linked_connections(db)
    return {r["user_id"]: r["state_dir"] for r in rows}


async def _reconcile(conf: dict[str, str]) -> None:
    desired = await _desired()

    # 1. Stop the ones that should no longer be running.
    for user_id in list(_children):
        if user_id not in desired:
            await _stop(user_id, reason="no longer linked")

    # 2. Reap the dead and put them on backoff.
    now = time.monotonic()
    for user_id, child in list(_children.items()):
        if child.proc.returncode is not None:
            code = child.proc.returncode
            del _children[user_id]
            if now - child.started_at >= HEALTHY_UPTIME:
                _backoff.pop(user_id, None)  # lived long enough, forget the backoff
            log.warning("sync for user=%s exited code=%s", user_id, code)
            _schedule_backoff(user_id)

    # 3. Start what is missing, respecting backoff.
    for user_id, state_dir in desired.items():
        if user_id in _children:
            continue
        retry_at = _backoff.get(user_id, (0.0, 0.0))[0]
        if now < retry_at:
            continue
        try:
            await _spawn(user_id, state_dir, conf)
        except Exception:  # noqa: BLE001
            log.exception("failed to spawn sync for user=%s", user_id)
            _schedule_backoff(user_id)


async def _shutdown() -> None:
    global _stopping
    _stopping = True
    log.info("shutting down — stopping %d children", len(_children))
    await asyncio.gather(
        *(_stop(uid, reason="shutdown") for uid in list(_children)),
        return_exceptions=True,
    )


def _poll_interval(conf: dict[str, str]) -> float:
    try:
        return max(1.0, float(conf["supervisor_poll_sec"]))
    except (KeyError, TypeError, ValueError):
        return 5.0


async def main() -> None:
    # The schema may not exist yet if the supervisor won the race to start.
    await dbm.migrate()
    log.info("supervisor up")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_shutdown()))

    while not _stopping:
        interval = 5.0
        try:
            conf = await settings_store.load(force=True)
            interval = _poll_interval(conf)
            await _reconcile(conf)
        except Exception:  # noqa: BLE001 — one bad pass must not kill the supervisor
            log.exception("reconcile loop error")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
