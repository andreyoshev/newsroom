"""A thin wrapper around the `wacli` CLI: run it, parse its JSON envelope.

Every call here is read-only (`messages list`, `chats list`, `doctor`). One
binary serves every account: each call is aimed at a specific user's state
directory through `HOME` / `XDG_STATE_HOME`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


class WacliError(RuntimeError):
    """A failed wacli call: either a non-zero exit or an error in the JSON."""


def env_for(state_dir: str) -> dict[str, str]:
    """Environment that points wacli at one user's store."""
    return {
        **os.environ,
        "HOME": state_dir,
        "XDG_STATE_HOME": f"{state_dir}/.local/state",
        "WACLI_READONLY": "1",
    }


async def _run(state_dir: str, *args: str, timeout: float = 30.0) -> dict[str, Any]:
    """Run `wacli --json <args>` against state_dir and return the envelope's data."""
    full_args = ("wacli", "--json", *args)
    log.debug("wacli call (%s): %s", state_dir, " ".join(full_args))
    proc = await asyncio.create_subprocess_exec(
        *full_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env_for(state_dir),
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise WacliError(f"wacli {' '.join(args)} timed out after {timeout}s")

    if proc.returncode != 0:
        raise WacliError(
            f"wacli {' '.join(args)} exited {proc.returncode}: "
            f"{stderr_bytes.decode(errors='replace')[:500]}"
        )

    stdout = stdout_bytes.decode(errors="replace")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise WacliError(f"wacli returned non-JSON: {stdout[:300]}") from exc

    if not payload.get("success"):
        raise WacliError(f"wacli error: {payload.get('error')}")

    return payload.get("data") or {}


async def list_chats(state_dir: str, limit: int = 1000) -> list[dict[str, Any]]:
    data = await _run(state_dir, "chats", "list", "--limit", str(limit), timeout=10.0)
    if isinstance(data, list):
        return data
    return data.get("chats", []) if isinstance(data, dict) else []


async def list_messages_since(
    state_dir: str,
    after_iso: str,
    *,
    chat_jid: str | None = None,
    limit: int = 5000,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Messages since after_iso (RFC3339 UTC, e.g. `2026-05-11T00:00:00Z`)."""
    args = ["messages", "list", "--after", after_iso, "--limit", str(limit), "--asc"]
    if chat_jid:
        args.extend(["--chat", chat_jid])
    data = await _run(state_dir, *args, timeout=timeout)
    messages = data.get("messages", []) if isinstance(data, dict) else []
    return messages if isinstance(messages, list) else []


async def doctor(state_dir: str) -> dict[str, Any] | None:
    """`wacli doctor` for state_dir; None when the call failed."""
    try:
        return await _run(state_dir, "doctor", timeout=8.0)
    except WacliError:
        return None
