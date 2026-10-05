"""deskhand's own tools, offered next to the driver's.

ask_user lets Claude ask the person at the terminal for a choice, a code or a
go-ahead. shell runs a command in the sandbox; the Mac never gets one. Both go
through the same hooks as driver calls (as a one-block text result), so they
show up in the terminal and the trace like any other tool call. Each is declared
from the first request of a run, since the tool list can't change mid-run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from anthropic.lib.tools import BetaAsyncFunctionTool, beta_async_tool
from mcp.types import CallToolResult, TextContent

from deskhand.bridge import CallHook
from deskhand.targets.base import ShellRunner


async def _observed(
    name: str,
    arguments: dict[str, Any],
    hooks: Sequence[CallHook],
    call: Callable[[], Awaitable[tuple[str, bool]]],
) -> str:
    for hook in hooks:
        await hook.before_call(name, arguments)
    text, is_error = await call()
    result = CallToolResult(content=[TextContent(type="text", text=text)], is_error=is_error)
    for hook in hooks:
        await hook.after_call(name, arguments, result, None)
    return text


def ask_user_tool(
    ask: Callable[[str], Awaitable[str]], hooks: Sequence[CallHook]
) -> BetaAsyncFunctionTool[Any]:
    @beta_async_tool
    async def ask_user(question: str) -> str:
        """Ask the user a question in their terminal and wait for the answer.

        Use it for information only the user has (a choice, a one-time code) and to
        confirm before anything hard to undo.

        Args:
            question: The question, with enough context to answer without seeing the screen.
        """

        async def call() -> tuple[str, bool]:
            answer = (await ask(question)).strip()
            return (answer or "(the user gave no answer)"), False

        return await _observed("ask_user", {"question": question}, hooks, call)

    return ask_user


def shell_tool(run_shell: ShellRunner, hooks: Sequence[CallHook]) -> BetaAsyncFunctionTool[Any]:
    @beta_async_tool
    async def shell(command: str) -> str:
        """Run a shell command in the sandbox as the desktop user.

        Returns the exit code, stdout and stderr. Non-interactive (sh -c), times out
        after 120 seconds; long output is cut.

        Args:
            command: The command line to run.
        """
        return await _observed("shell", {"command": command}, hooks, lambda: run_shell(command))

    return shell
