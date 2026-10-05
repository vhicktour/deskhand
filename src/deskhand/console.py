"""What a run looks like in the terminal.

One line per tool call with its key arguments, Claude's progress notes and
replies as they arrive, a running step and cost count, and a closing summary.
It is also the CallHook that prints driver calls, and it asks the user
ask_user's questions. The question is read on a daemon thread, so Ctrl+C still
stops the run while the prompt is waiting.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult
from rich.console import Console
from rich.markup import escape

from deskhand.runlog import RunSummary

# Arguments worth showing on a tool line, in this order.
_SHOWN_ARGS = (
    "pid",
    "window_id",
    "element_token",
    "x",
    "y",
    "text",
    "key",
    "keys",
    "direction",
    "url",
    "name",
    "bundle_id",
    "launch_path",
    "query",
    "command",
)

_STATUS_STYLE = {"done": "green", "interrupted": "yellow", "error": "red", "refused": "red"}


def _short(value: Any, limit: int = 40) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def args_summary(arguments: dict[str, Any]) -> str:
    """The few arguments that say what a call does, e.g. `pid=412 text="hello"`."""
    target = arguments.get("target")
    parts = []
    if isinstance(target, dict) and target.get("kind") == "desktop":
        parts.append("desktop")
    elif isinstance(target, dict):
        parts += [f"{k}={target[k]}" for k in ("pid", "window_id") if k in target]
    for key in _SHOWN_ARGS:
        if key in arguments:
            value = arguments[key]
            shown = (
                f'"{_short(value)}"'
                if isinstance(value, str) and key != "element_token"
                else _short(value, 12)
            )
            parts.append(f"{key}={shown}")
    return " ".join(parts)


class ConsoleView:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def header(
        self, task: str, target: str, model: str, effort: str, max_steps: int, max_cost: float
    ) -> None:
        self.console.print(f"[bold]deskhand[/] · {escape(task)}")
        limits = f"up to {max_steps} steps / ${max_cost:.2f}"
        self.console.print(f"[dim]{target} · {model} · effort {effort} · {limits}[/]")

    def info(self, text: str) -> None:
        self.console.print(f"[dim]{escape(text)}[/]")

    def turn(self, step: int, notes: list[str], texts: list[str], cost: float) -> None:
        for note in notes:
            self.console.print(f"[italic dim]{escape(note)}[/]")
        for text in texts:
            self.console.print(escape(text))
        self.console.print(f"[dim]step {step} · ${cost:.2f}[/]")

    async def before_call(self, name: str, arguments: dict[str, Any]) -> None:
        self.console.print(f"  [cyan]→ {name}[/] [dim]{escape(args_summary(arguments))}[/]")

    async def after_call(
        self,
        name: str,
        arguments: dict[str, Any],
        result: CallToolResult | None,
        error: str | None,
    ) -> None:
        if error:
            self.console.print(f"    [red]{escape(error)}[/]")
        elif result is not None and result.is_error:
            first = next((b.text for b in result.content if b.type == "text"), "error")
            self.console.print(
                f"    [red]{escape(_short(first.splitlines()[0] if first else 'error', 160))}[/]"
            )

    async def ask(self, question: str) -> str:
        """Ask the user in the terminal without blocking Ctrl+C."""
        self.console.print(f"\n[bold yellow]Claude asks:[/] {escape(question)}")
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[str] = loop.create_future()

        def read() -> None:
            try:
                text = input("> ")
            except (EOFError, KeyboardInterrupt):
                text = ""
            loop.call_soon_threadsafe(lambda: answer.done() or answer.set_result(text))

        threading.Thread(target=read, daemon=True).start()
        return await answer

    def finish(self, summary: RunSummary, run_dir: Path) -> None:
        style = _STATUS_STYLE.get(summary.status, "yellow")
        self.console.print()
        if summary.result:
            self.console.print(escape(summary.result))
        if summary.error:
            self.console.print(f"[red]{escape(summary.error)}[/]")
        counts = f"{summary.steps} steps · {summary.tool_calls} tool calls"
        self.console.print(f"[{style}]{summary.status}[/] · {counts} · ${summary.cost_usd:.2f}")
        self.console.print(f"[dim]report: {run_dir / 'report.html'}[/]")
