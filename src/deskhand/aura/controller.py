"""Keeps the aura on the window the agent is working on, and its cursor on the spot.

AuraController is a CallHook. Before each driver call it tells the overlay
process (deskhand.aura.overlay, started with this same Python) which window the
call targets, and where on screen a pointer call lands, worked out by ScreenMap
from the snapshots seen so far. Full-display captures hide everything first and
wait for the overlay to confirm, so Claude never sees it in a screenshot.
Leaving the controller closes the overlay's stdin, which ends it.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult

from deskhand.aura.points import ScreenMap, Where, target_window

# Driver calls that capture the whole display, where the aura would show up.
# Window captures (get_window_state, zoom) never include other windows.
FULL_DISPLAY_CAPTURES = frozenset({"get_desktop_state"})
# Calls that click (the cursor ripples) and other calls that point somewhere.
CLICKS = frozenset({"click", "double_click", "right_click"})
POINTING = frozenset({"move_cursor", "scroll", "drag", "type_text", "press_key", "set_value"})
HIDE_ACK_TIMEOUT_S = 0.5


class AuraController:
    def __init__(self, log_path: Path | None = None) -> None:
        self._log_path = log_path
        self._proc: asyncio.subprocess.Process | None = None
        self._current: Where | None = None
        self._screen = ScreenMap()

    async def __aenter__(self) -> AuraController:
        stderr: Any = subprocess.DEVNULL
        if self._log_path is not None:
            stderr = self._log_path.open("ab")
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "deskhand.aura",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
        )
        if stderr is not subprocess.DEVNULL:
            stderr.close()  # the child has its own copy
        return self

    async def __aexit__(self, *exc: object) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def before_call(self, name: str, arguments: dict[str, Any]) -> None:
        if name in FULL_DISPLAY_CAPTURES:
            await self._send("hide", wait_for_ack=True)
            return
        where = target_window(arguments)
        if where is not None and where != self._current:
            self._current = where
            await self._send("display" if where == "display" else f"window {where}")
        if name in CLICKS or name in POINTING:
            spot = self._screen.locate(arguments)
            if spot is not None:
                verb = "click" if name in CLICKS else "point"
                await self._send(f"{verb} {spot[0]:.1f} {spot[1]:.1f}")

    async def after_call(
        self,
        name: str,
        arguments: dict[str, Any],
        result: CallToolResult | None,
        error: str | None,
    ) -> None:
        if result is not None and not result.is_error:
            self._screen.learn(name, result.structured_content)
        if name in FULL_DISPLAY_CAPTURES:
            await self._send("show")

    async def _send(self, command: str, wait_for_ack: bool = False) -> None:
        """Send one command; the aura is decoration, so a dead overlay is ignored."""
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(f"{command}\n".encode())
            await proc.stdin.drain()
            if wait_for_ack and proc.stdout is not None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.stdout.readline(), HIDE_ACK_TIMEOUT_S)
