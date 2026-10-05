"""deskhand's MCP server for Claude Code: `deskhand mcp`, over stdio.

Sandbox tools let Claude make, use and remove Linux desktop sandboxes (at most
three at once; idle ones are swept), each shown in the native viewer window.
Task tools hand a whole job to deskhand's own agent, in a sandbox or on this Mac,
in the background: start it, check on it (optionally waiting), stop it. Results
point at the run's report so Claude can read what happened. The tool docstrings
below are what Claude reads, so they say when and how to use each tool.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from deskhand import sandboxes, tasks, viewer
from deskhand.models import MODELS

EFFORTS = ("low", "medium", "high", "xhigh", "max")

server = MCPServer(
    "deskhand",
    instructions=(
        "deskhand gives you computers to use: disposable Linux desktop sandboxes "
        "(at most 3 at once) and this Mac. Prefer sandboxes; use the Mac only when "
        "the user asks for something on their Mac. Delete sandboxes you're done with."
    ),
)


@server.tool()
async def sandbox_create(owner: str = "claude") -> dict[str, Any]:
    """Start a new Linux desktop sandbox (XFCE, Chromium and Firefox installed).

    Use one sandbox per agent or experiment, and name the owner (for example the
    subagent or feature you're testing) so it's clear whose it is. At most 3 run at
    once; sandboxes idle for 30 minutes are deleted. The sandbox appears in the
    deskhand viewer window. Delete it with sandbox_delete when you're done.
    """
    record = await sandboxes.create(owner)
    viewer.show()
    return {"name": record.name, "owner": record.owner, "viewer_url": record.viewer_url}


@server.tool()
async def sandbox_list() -> list[dict[str, Any]]:
    """List deskhand's running sandboxes with their owners and last use."""
    await sandboxes.sweep()
    alive = set(await sandboxes.running())
    return [
        {"name": r.name, "owner": r.owner, "created_at": r.created_at, "last_used": r.last_used}
        for r in sandboxes.records()
        if r.name in alive
    ]


@server.tool()
async def sandbox_delete(name: str) -> str:
    """Delete a sandbox and everything in it."""
    await sandboxes.delete(name)
    return f"Deleted {name}."


@server.tool()
async def sandbox_shell(name: str, command: str, timeout_s: int = 120) -> str:
    """Run a shell command in a sandbox as the desktop user (sh -c, non-interactive).

    Good for setup (installing packages, copying in a build, starting a server),
    for checking results, and for reading logs. GUI apps need DISPLAY=:1.
    Returns the exit code and output (long output is cut).
    """
    text, _ = await sandboxes.shell(name, command, timeout_s)
    return text


@server.tool()
async def sandbox_screenshot(name: str) -> Image:
    """A screenshot of a sandbox's whole desktop, to see its state yourself."""
    return Image(data=await sandboxes.screenshot(name), format="png")


@server.tool()
async def task_start(
    task: str,
    on: str = "sandbox",
    sandbox: str | None = None,
    model: str = "opus",
    effort: str = "medium",
    max_steps: int = 50,
    max_cost: float = 2.0,
) -> dict[str, Any]:
    """Hand a whole computer task to deskhand's agent; it runs in the background.

    Describe the task in plain English, with what to check at the end. on="sandbox"
    runs in the named sandbox (left running afterwards so you can inspect it) or,
    without one, in a fresh sandbox deleted at the end. on="mac" drives this Mac's
    real apps with an orange glow on the window being worked on: only when the user
    asked for something on their Mac. model is opus (default), sonnet or fable. The
    run stops at max_steps model turns or max_cost dollars. Returns a run_id for
    task_status and task_stop.
    """
    if on not in ("sandbox", "mac"):
        raise ValueError('on must be "sandbox" or "mac"')
    if on == "mac" and sandbox:
        raise ValueError("a Mac run doesn't take a sandbox")
    if model not in {alias.value for alias in MODELS}:
        raise ValueError(f"model must be one of {', '.join(a.value for a in MODELS)}")
    if effort not in EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
    if max_steps < 1 or max_cost <= 0:
        raise ValueError("max_steps and max_cost must be positive")
    run_id = tasks.start(
        task,
        on=on,
        sandbox=sandbox,
        model=model,
        effort=effort,
        max_steps=max_steps,
        max_cost=max_cost,
    )
    return {"run_id": run_id, "status": "running"}


@server.tool()
async def task_status(run_id: str, wait_s: int = 0) -> dict[str, Any]:
    """A background run's state: running (with its latest notes) or how it ended.

    wait_s (up to 600) waits for the run to end first. When it has ended you get
    the status (done, step_limit, cost_limit, refused, error, interrupted or
    crashed), the result or error, steps, cost and the path of report.html,
    whose run folder also holds trace.jsonl and the screenshots.
    """
    return (await tasks.wait(run_id, wait_s)).to_dict()


@server.tool()
async def task_stop(run_id: str) -> str:
    """Stop a background run, as with Ctrl+C: it still writes its summary."""
    return "Stopping." if tasks.stop(run_id) else "That run isn't running."


def main() -> None:
    server.run("stdio")
