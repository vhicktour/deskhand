"""A Linux desktop sandbox on this Mac's Docker, for one run.

A run either makes its own sandbox through the sandbox manager (it counts toward
the cap and is deleted afterwards unless kept) or works in a named one that
already exists, made by `deskhand sandbox create` or the MCP server, which it
leaves running. Either way it connects to the Cua Driver in the image through
cua-spacesd's MCP route, shows the sandbox in the viewer window, and offers the
sandbox's shell: the one place a shell is safe, since the sandbox is isolated
and the Mac is not.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx2
from cua_sandbox import Sandbox
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from deskhand import sandboxes, viewer
from deskhand.targets.base import Session, ShellRunner, TargetError

DOCKER_HINT = "Is Docker Desktop running? `deskhand doctor` checks."
KEEPALIVE_S = 60


def _shell_runner(sb: Sandbox) -> ShellRunner:
    async def run(command: str) -> tuple[str, bool]:
        result = await sb.shell.run(command, timeout=sandboxes.SHELL_TIMEOUT_S)
        return sandboxes.format_shell(result.returncode, result.stdout, result.stderr)

    return run


class _KeepAlive:
    """A CallHook that marks the sandbox as in use, so the idle sweep leaves it alone."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._last = 0.0

    async def before_call(self, name: str, arguments: dict[str, Any]) -> None:
        if time.monotonic() - self._last > KEEPALIVE_S:
            self._last = time.monotonic()
            sandboxes.touch(self._name)

    async def after_call(
        self, name: str, arguments: dict[str, Any], result: CallToolResult | None, error: str | None
    ) -> None:
        return None


async def _driver_session(sb: Sandbox, stack: AsyncExitStack) -> Client:
    """One MCP session to the sandbox's Cua Driver, kept for the whole run.

    Cua Driver ties screenshots and element snapshots to the connection that made
    them. The 2026-07-28 protocol the MCP client negotiates by default is stateless
    over HTTP, so every call reached the driver as a new connection and every
    pixel action (click, scroll at a point, zoom) failed with "No current snapshot
    … owned by this session". mode="legacy" keeps one session, as on the Mac's
    stdio connection. sb.mcp() can't pass the mode, so the client is built here.
    """
    config = await sb.mcp_config("env")
    http = httpx2.AsyncClient(headers=config["headers"], timeout=httpx2.Timeout(30.0, read=300.0))
    await stack.enter_async_context(http)
    transport = streamable_http_client(config["url"], http_client=http)
    return await stack.enter_async_context(Client(transport, mode="legacy"))


async def _make(info: Callable[[str], None]) -> str:
    info("Starting a Linux sandbox (the first one downloads about 4 GB)…")
    try:
        return (await sandboxes.create(owner="run")).name
    except sandboxes.SandboxLimitError as exc:
        raise TargetError(str(exc)) from exc
    except Exception as exc:
        raise TargetError(f"Couldn't start the sandbox: {exc}\n{DOCKER_HINT}") from exc


@asynccontextmanager
async def open_sandbox(
    *, name: str | None, keep: bool, view: bool, info: Callable[[str], None]
) -> AsyncIterator[Session]:
    """A session in sandbox `name`, or in a new one when name is None."""
    made_here = name is None
    if name is None:
        name = await _make(info)
    try:
        async with AsyncExitStack() as stack:
            try:
                sb = await stack.enter_async_context(Sandbox.connect(name, local=True))
            except Exception as exc:
                raise TargetError(f"No sandbox {name} to connect to ({exc}).") from exc
            url = await sb.viewer_url()
            sandboxes.save_viewer_url(name, url)
            info(f"Sandbox {name} · viewer {url}")
            if view:
                viewer.show()
            client = await _driver_session(sb, stack)
            yield Session(
                name="sandbox",
                driver=client,
                hooks=[_KeepAlive(name)],
                viewer_url=url,
                run_shell=_shell_runner(sb),
            )
    finally:
        if made_here and keep:
            info(f"Kept sandbox {name}. Delete it with: deskhand sandbox rm {name}")
        elif made_here:
            try:
                await sandboxes.delete(name)
            except Exception as exc:
                info(f"Couldn't delete sandbox {name} ({exc}). Try: deskhand sandbox rm {name}")
