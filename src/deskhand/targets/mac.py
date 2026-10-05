"""This Mac, driven through the installed Cua Driver.

`cua-driver mcp` speaks MCP over stdio and hands the work to the CuaDriver.app
daemon, which holds the Accessibility and Screen Recording grants (they belong
to the app, not to the terminal that started it). Permissions are checked before
the run starts, so a missing grant fails with the fix instead of halfway through
a task. The driver's own log goes to the run folder rather than the terminal,
and the aura runs alongside unless it is turned off. With the aura on, every
driver call runs in one session whose own agent cursor is switched off: the
driver draws that cursor only on the main display and in window coordinates, so
it showed up on the wrong screen. The aura draws an accurate one instead. Each
run starts its own session and ends it on the way out (see open_mac).
"""

from __future__ import annotations

import contextlib
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from deskhand.aura import AuraController
from deskhand.bridge import CallHook
from deskhand.targets.base import Session, TargetError

SESSION_PREFIX = "deskhand"
INSTALL_HINT = 'Install Cua Driver: /bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"'
GRANT_HINT = (
    "Run `cua-driver permissions grant` and accept its prompts (Accessibility, "
    "Screen & System Audio Recording, direct capture), then run again."
)


def find_driver() -> str | None:
    found = shutil.which("cua-driver")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "cua-driver"
    return str(fallback) if fallback.exists() else None


async def _check_permissions(client: Client) -> None:
    result = await client.call_tool("check_permissions", {})
    if result.is_error:
        detail = " ".join(b.text for b in result.content if b.type == "text")
        raise TargetError(f"Cua Driver can't control this Mac yet: {detail}\n{GRANT_HINT}")


async def _end_session(client: Client, session: str) -> None:
    # Teardown must not turn a finished run into a failure; if the connection is
    # already gone, the driver cleans the session up itself.
    with contextlib.suppress(Exception):
        await client.call_tool("end_session", {"session": session})


@asynccontextmanager
async def open_mac(*, aura: bool, log_dir: Path) -> AsyncIterator[Session]:
    """Connect to Cua Driver on this Mac; its log and the aura's go in log_dir."""
    binary = find_driver()
    if binary is None:
        raise TargetError(f"Cua Driver isn't installed. {INSTALL_HINT}")
    params = StdioServerParameters(command=binary, args=["mcp"])
    with (log_dir / "driver.log").open("a", encoding="utf-8") as errlog:
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(Client(stdio_client(params, errlog=errlog)))
            await _check_permissions(client)
            if not aura:
                yield Session(name="mac", driver=client)
                return
            # A driver session name belongs to the connection that made it; once that
            # connection is gone the name is dead, and a new one can't revive it
            # ("not available to this transport"). With one fixed name, every Mac run
            # after the first failed each call with "session has ended". So each run
            # starts a session of its own and ends it, which runs the driver's
            # cleanup (cursor, recording) for it.
            session = f"{SESSION_PREFIX}-{uuid.uuid4().hex[:8]}"
            await client.call_tool("start_session", {"session": session})
            stack.push_async_callback(_end_session, client, session)
            off = {"session": session, "enabled": False}
            await client.call_tool("set_agent_cursor_enabled", off)
            controller = AuraController(log_path=log_dir / "aura.log")
            hooks: list[CallHook] = [await stack.enter_async_context(controller)]
            yield Session(name="mac", driver=client, hooks=hooks, driver_session=session)
