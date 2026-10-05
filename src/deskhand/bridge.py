"""Cua Driver's MCP tools, offered to Claude as ordinary tools.

Both targets expose the same driver over MCP. The tool list is fetched once per
run, cut down to CURATED_TOOLS and sorted by name, so the tools array is
byte-identical on every request (the thinking-block and cache checks depend on
it). Every call passes through DriverBridge, which lets hooks see it before and
after (the run log saves screenshots, the aura follows the target window), lets
an optional gate (the guard) refuse it or add to its result, and turns transport
failures into errors Claude can read and recover from.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, cast

from anthropic.lib.tools import BetaAsyncFunctionTool, ToolError
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

# The driver tools useful for doing tasks: observing, input, apps and windows,
# and the browser. Left out: session and recording plumbing, cursor styling,
# config, diagnostics, deprecated aliases, Linux-only raw pointer tools, and
# kill_app (quitting through the app is safer on a real Mac).
CURATED_TOOLS = frozenset(
    {
        # observe
        "get_desktop_state",
        "get_window_state",
        "get_accessibility_tree",
        "get_screen_size",
        "zoom",
        "verify_state",
        # apps and windows
        "list_apps",
        "launch_app",
        "list_windows",
        "bring_to_front",
        "set_window_frame",
        "invoke_menu",
        # input
        "click",
        "double_click",
        "right_click",
        "drag",
        "scroll",
        "move_cursor",
        "type_text",
        "press_key",
        "hotkey",
        "set_value",
        "clipboard_read",
        "clipboard_write",
        # browser
        "get_browser_state",
        "browser_prepare",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_pointer",
        "browser_dialog",
        "browser_set_input_files",
        "browser_download",
    }
)


# Arguments a call gets unless Claude sets them. A full-display capture on a
# Retina screen is 4112 px wide, beyond what Claude reads unscaled, so its pixel
# coordinates would come back short; capped like window captures (1568 px), the
# driver maps them back to the full-size frame itself.
DEFAULT_ARGUMENTS: dict[str, dict[str, Any]] = {
    "get_desktop_state": {"max_image_dimension": 1568},
}


class DriverClient(Protocol):
    """What deskhand needs from an MCP connection to Cua Driver (mcp.Client fits)."""

    async def list_tools(self, *, cursor: str | None = None) -> ListToolsResult: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> CallToolResult: ...


class CallHook(Protocol):
    """Something that watches driver calls: the run log, the console, the aura."""

    async def before_call(self, name: str, arguments: dict[str, Any]) -> None: ...

    async def after_call(
        self,
        name: str,
        arguments: dict[str, Any],
        result: CallToolResult | None,
        error: str | None,
    ) -> None: ...


class CallGate(Protocol):
    """Something that may stop a driver call or add to its result: the guard."""

    async def check(self, name: str, arguments: dict[str, Any]) -> str | None:
        """None lets the call run; a string refuses it and is what Claude reads."""
        ...

    async def review(
        self, name: str, arguments: dict[str, Any], result: CallToolResult
    ) -> CallToolResult: ...


class DriverBridge:
    """The MCP client as async_mcp_tool sees it, with hooks around each call.

    async_mcp_tool only ever calls `call_tool(name=..., arguments=...)` on the
    client it is given, so this stands in for it and keeps the SDK's MCP-to-Claude
    result conversion (text and image blocks, errors) in one place. With a
    `session` label, every call that accepts one runs in that driver session,
    whatever label Claude picked: snapshots and element tokens stay in one
    session, and the target can manage that session's cursor. A refused call
    never reaches the driver: the hooks see it only afterwards, as an error, so
    the aura doesn't point at something that won't be clicked.
    """

    def __init__(
        self,
        client: DriverClient,
        hooks: Sequence[CallHook],
        session: str | None = None,
        session_tools: frozenset[str] = frozenset(),
        gate: CallGate | None = None,
    ) -> None:
        self._client = client
        self._hooks = list(hooks)
        self._session = session
        self._session_tools = session_tools
        self._gate = gate

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        args = {**DEFAULT_ARGUMENTS.get(name, {}), **(arguments or {})}
        if self._session is not None and name in self._session_tools:
            args["session"] = self._session
        refusal = await self._gate.check(name, args) if self._gate else None
        if refusal is not None:
            refused = CallToolResult(
                content=[TextContent(type="text", text=refusal)], is_error=True
            )
            for hook in self._hooks:
                await hook.after_call(name, args, refused, None)
            return refused
        for hook in self._hooks:
            await hook.before_call(name, args)
        try:
            result = await self._client.call_tool(name, args)
        except Exception as exc:  # transport or protocol failure, not a tool refusal
            message = f"{name} failed: {exc}"
            for hook in self._hooks:
                await hook.after_call(name, args, None, message)
            raise ToolError(message) from exc
        if self._gate:
            result = await self._gate.review(name, args, result)
        for hook in self._hooks:
            await hook.after_call(name, args, result, None)
        return result


async def list_driver_tools(client: DriverClient) -> list[Tool]:
    """The curated driver tools this target offers, sorted by name."""
    tools: list[Tool] = []
    cursor: str | None = None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(t for t in page.tools if t.name in CURATED_TOOLS)
        cursor = page.next_cursor
        if not cursor:
            break
    return sorted(tools, key=lambda t: t.name)


async def driver_tools(
    client: DriverClient,
    hooks: Sequence[CallHook],
    session: str | None = None,
    gate: CallGate | None = None,
) -> list[BetaAsyncFunctionTool[Any]]:
    """Claude tools for the target's driver, calling through a hooked bridge."""
    tools = await list_driver_tools(client)
    session_tools = frozenset(
        t.name for t in tools if "session" in t.input_schema.get("properties", {})
    )
    bridge = cast(Any, DriverBridge(client, hooks, session, session_tools, gate))
    return [async_mcp_tool(tool, bridge) for tool in tools]
