"""Named Linux sandboxes that outlive a single call: create, use, list, delete.

The CLI, the MCP server and task runs all go through here. Sandboxes are local
cua-sandbox containers named deskhand-<owner>-<time>. A registry file keeps who
made each one, when it was last used and its viewer link; that drives the cap
(MAX_SANDBOXES at once) and the idle sweep (IDLE_MINUTES), and feeds the native
viewer. Whether a sandbox exists comes from cua itself; the registry only adds
context and is rewritten atomically under a lock, since several processes use it.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from cua_sandbox import Image, Sandbox

from deskhand.paths import sandboxes_file

PREFIX = "deskhand-"
MAX_SANDBOXES = 3
IDLE_MINUTES = 30
SHELL_TIMEOUT_S = 120
SHELL_OUTPUT_CHARS = 20_000


class SandboxLimitError(Exception):
    """The cap is reached; the message lists the sandboxes that hold the slots."""


@dataclass
class SandboxRecord:
    name: str
    owner: str
    created_at: str
    last_used: str
    viewer_url: str = ""
    viewer_url_at: str = ""


def _now() -> datetime:
    return datetime.now().astimezone()


@contextmanager
def _registry() -> Iterator[dict[str, SandboxRecord]]:
    """The registry, locked for read-modify-write and saved on the way out."""
    path = sandboxes_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / "sandboxes.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records: dict[str, SandboxRecord] = {}
        if path.exists():
            for name, data in json.loads(path.read_text(encoding="utf-8")).items():
                records[name] = SandboxRecord(**data)
        yield records
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({n: asdict(r) for n, r in records.items()}, indent=2))
        os.replace(temp, path)


def records() -> list[SandboxRecord]:
    with _registry() as registry:
        return sorted(registry.values(), key=lambda r: r.created_at)


def touch(name: str) -> None:
    with _registry() as registry:
        if name in registry:
            registry[name].last_used = _now().isoformat(timespec="seconds")


def _new_name(owner: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", owner.lower()).strip("-")[:20] or "claude"
    return f"{PREFIX}{slug}-{_now():%H%M%S}"


async def running() -> list[str]:
    """deskhand's sandboxes that exist right now, by name."""
    infos = await Sandbox.list(local=True)
    return sorted(i.name for i in infos if i.name and i.name.startswith(PREFIX))


async def create(owner: str) -> SandboxRecord:
    """A new Linux desktop sandbox, ready for the driver; raises SandboxLimitError at the cap."""
    await sweep()
    taken = await running()
    if len(taken) >= MAX_SANDBOXES:
        raise SandboxLimitError(
            f"{MAX_SANDBOXES} sandboxes are already running ({', '.join(taken)}). "
            "Delete one you're done with first."
        )
    name = _new_name(owner)
    sb = await Sandbox.create(Image.linux(), local=True, name=name)
    try:
        url = await sb.viewer_url()
    finally:
        await sb.disconnect()
    stamp = _now().isoformat(timespec="seconds")
    record = SandboxRecord(name, owner, stamp, stamp, url, stamp)
    with _registry() as registry:
        registry[name] = record
    return record


async def delete(name: str) -> None:
    try:
        await Sandbox.delete(name, local=True)
    finally:
        with _registry() as registry:
            registry.pop(name, None)


def format_shell(returncode: int, stdout: str, stderr: str) -> tuple[str, bool]:
    """A command's outcome as Claude reads it, cut to SHELL_OUTPUT_CHARS."""
    text = f"exit code {returncode}\n{stdout}"
    if stderr:
        text += f"\n[stderr]\n{stderr}"
    if len(text) > SHELL_OUTPUT_CHARS:
        cut = len(text) - SHELL_OUTPUT_CHARS
        text = f"{text[:SHELL_OUTPUT_CHARS]}\n… ({cut:,} more characters cut)"
    return text, returncode != 0


async def shell(name: str, command: str, timeout_s: int = SHELL_TIMEOUT_S) -> tuple[str, bool]:
    touch(name)
    async with Sandbox.connect(name, local=True) as sb:
        result = await sb.shell.run(command, timeout=timeout_s)
    return format_shell(result.returncode, result.stdout, result.stderr)


async def screenshot(name: str) -> bytes:
    touch(name)
    async with Sandbox.connect(name, local=True) as sb:
        return await sb.screenshot()


def save_viewer_url(name: str, url: str) -> None:
    with _registry() as registry:
        if name in registry:
            registry[name].viewer_url = url
            registry[name].viewer_url_at = _now().isoformat(timespec="seconds")


async def viewer_url(name: str) -> str:
    """A fresh viewer link (they expire after an hour), saved for the viewer window."""
    async with Sandbox.connect(name, local=True) as sb:
        url = await sb.viewer_url()
    save_viewer_url(name, url)
    return url


async def sweep(idle_minutes: int = IDLE_MINUTES) -> list[str]:
    """Delete sandboxes idle for idle_minutes and forget ones that no longer exist."""
    alive = set(await running())
    cutoff = _now() - timedelta(minutes=idle_minutes)
    with _registry() as registry:
        for name in [n for n in registry if n not in alive]:
            del registry[name]
        idle = [r.name for r in registry.values() if datetime.fromisoformat(r.last_used) < cutoff]
    for name in idle:
        await delete(name)
    return idle
