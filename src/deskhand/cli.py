"""The deskhand command line.

`run` opens the chosen computer, hands the task to the agent loop and prints a
summary; `runs` and `show` look back at saved runs; `sandbox` manages named
sandboxes; `viewer` opens the sandbox window; `mcp` serves deskhand to Claude
Code; `doctor` checks the setup. Library logs (Anthropic SDK, MCP, cua) go to the
run folder, not the terminal. Exit codes for `run`: 0 when the task finished,
130 after Ctrl+C, 1 for anything else (errors, limits, refusals).
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from deskhand import mcp_server, sandboxes, viewer
from deskhand.agent import Limits, run_task
from deskhand.console import ConsoleView
from deskhand.doctor import run_checks
from deskhand.models import DEFAULT_MODEL, MODELS, ModelAlias, ModelSpec
from deskhand.paths import runs_root
from deskhand.runlog import RunLog, RunSummary, find_run, list_runs, load_summary
from deskhand.targets import TargetError
from deskhand.targets.mac import open_mac
from deskhand.targets.sandbox import open_sandbox

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Give Claude a task; it does it on your Mac or in a Linux sandbox.",
)
sandbox_app = typer.Typer(no_args_is_help=True, help="Named Linux sandboxes (at most 3 at once).")
app.add_typer(sandbox_app, name="sandbox")


class On(StrEnum):
    sandbox = "sandbox"
    mac = "mac"


class Effort(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"
    xhigh = "xhigh"
    max = "max"


async def _run(
    *,
    task: str,
    on: On,
    spec: ModelSpec,
    effort: Effort,
    limits: Limits,
    sandbox: str | None,
    keep: bool,
    view: bool,
    aura: bool,
    log: RunLog,
    console: ConsoleView,
) -> RunSummary:
    if on is On.mac:
        target = open_mac(aura=aura, log_dir=log.dir)
    else:
        target = open_sandbox(name=sandbox, keep=keep, view=view, info=console.info)
    try:
        async with target as session:
            return await run_task(
                task=task,
                session=session,
                spec=spec,
                effort=effort.value,
                limits=limits,
                log=log,
                view=console,
            )
    except TargetError as exc:
        return log.finish(status="error", error=str(exc))


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="What to do, in plain English.")],
    on: Annotated[
        On, typer.Option(help="The computer: a local Linux sandbox, or this Mac.")
    ] = On.sandbox,
    model: Annotated[
        ModelAlias,
        typer.Option(help="opus: Claude Opus 5.5 · sonnet: Sonnet 5.5 · fable: Fable 5.1."),
    ] = DEFAULT_MODEL,
    effort: Annotated[
        Effort, typer.Option(help="How much Claude thinks per step.")
    ] = Effort.medium,
    max_steps: Annotated[int, typer.Option(min=1, help="Stop after this many model turns.")] = 50,
    max_cost: Annotated[
        float, typer.Option(min=0.01, help="Stop once the run has cost this many dollars.")
    ] = 2.0,
    sandbox: Annotated[
        str | None,
        typer.Option(
            help="Work in this existing sandbox (see `deskhand sandbox list`) and leave it running."
        ),
    ] = None,
    keep: Annotated[bool, typer.Option(help="Keep a new sandbox running afterwards.")] = False,
    view: Annotated[
        bool, typer.Option(help="Show the sandbox in the deskhand viewer window.")
    ] = True,
    aura: Annotated[
        bool, typer.Option(help="Orange glow and cursor on the window being worked on (Mac).")
    ] = True,
    run_id: Annotated[str | None, typer.Option(hidden=True)] = None,
) -> None:
    """Do TASK on a computer."""
    if sandbox and on is On.mac:
        raise typer.BadParameter("--sandbox only works with --on sandbox")
    spec = MODELS[model]
    console = ConsoleView()
    log = RunLog(runs_root(), task, on.value, spec.id, effort.value, run_id=run_id)
    logging.basicConfig(filename=log.dir / "deskhand.log", level=logging.INFO, force=True)
    console.header(task, on.value, spec.id, effort.value, max_steps, max_cost)
    try:
        summary = asyncio.run(
            _run(
                task=task,
                on=on,
                spec=spec,
                effort=effort,
                limits=Limits(max_steps=max_steps, max_cost=max_cost),
                sandbox=sandbox,
                keep=keep,
                view=view,
                aura=aura,
                log=log,
                console=console,
            )
        )
    except KeyboardInterrupt:
        summary = log.finish(status="interrupted", error="Stopped with Ctrl+C.")
    console.finish(summary, log.dir)
    raise typer.Exit(
        0 if summary.status == "done" else 130 if summary.status == "interrupted" else 1
    )


@app.command()
def runs(limit: Annotated[int, typer.Option(help="How many runs to list.")] = 20) -> None:
    """List past runs, newest first."""
    table = Table("Run", "Status", "Target", "Steps", "Cost", "Task", box=None, pad_edge=False)
    for summary in list_runs(runs_root())[:limit]:
        table.add_row(
            summary.id[:15],
            summary.status,
            summary.target,
            str(summary.steps),
            f"${summary.cost_usd:.2f}",
            escape(summary.task if len(summary.task) <= 60 else summary.task[:59] + "…"),
        )
    Console().print(table)


@app.command()
def show(
    run_id: Annotated[
        str, typer.Argument(help="A run id from `deskhand runs` (a unique prefix works).")
    ],
    open_report: Annotated[bool, typer.Option("--open/--no-open", help="Open report.html.")] = True,
) -> None:
    """Show a run's summary and open its report."""
    out = Console()
    path = find_run(runs_root(), run_id)
    if path is None:
        out.print(f"[red]No single run matches {escape(run_id)}.[/] See `deskhand runs`.")
        raise typer.Exit(1)
    summary = load_summary(path)
    out.print(f"[bold]{escape(summary.task)}[/]")
    out.print(
        f"{summary.status} · {summary.target} · {summary.model} · {summary.steps} steps · "
        f"{summary.tool_calls} tool calls · ${summary.cost_usd:.2f}"
    )
    for text in (summary.result, summary.error):
        if text:
            out.print(escape(text))
    report = path / "report.html"
    out.print(f"[dim]{report}[/]")
    if open_report and report.exists():
        typer.launch(str(report))


@sandbox_app.command("create")
def sandbox_create(
    owner: Annotated[str, typer.Option(help="Who it's for, shown in its name.")] = "you",
    view: Annotated[bool, typer.Option(help="Show it in the deskhand viewer window.")] = True,
) -> None:
    """Start a sandbox that stays until you delete it (or it sits idle 30 minutes)."""
    try:
        record = asyncio.run(sandboxes.create(owner))
    except sandboxes.SandboxLimitError as exc:
        Console().print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from exc
    if view:
        viewer.show()
    Console().print(record.name)


@sandbox_app.command("list")
def sandbox_list() -> None:
    """List running sandboxes."""
    alive = set(asyncio.run(sandboxes.running()))
    table = Table("Sandbox", "Owner", "Created", "Last used", box=None, pad_edge=False)
    for r in sandboxes.records():
        if r.name in alive:
            table.add_row(r.name, escape(r.owner), r.created_at[11:19], r.last_used[11:19])
    Console().print(table)


@sandbox_app.command("rm")
def sandbox_rm(
    name: Annotated[str, typer.Argument(help="A name from `deskhand sandbox list`.")],
) -> None:
    """Delete a sandbox."""
    asyncio.run(sandboxes.delete(name))
    Console().print(f"Deleted {escape(name)}.")


@sandbox_app.command("url")
def sandbox_url(
    name: Annotated[str, typer.Argument(help="A name from `deskhand sandbox list`.")],
) -> None:
    """Print a fresh viewer link for a sandbox (links last an hour)."""
    Console().print(asyncio.run(sandboxes.viewer_url(name)), soft_wrap=True)


@sandbox_app.command("sweep")
def sandbox_sweep(
    idle_minutes: Annotated[
        int, typer.Option(min=0, help="Delete sandboxes idle this long.")
    ] = sandboxes.IDLE_MINUTES,
) -> None:
    """Delete idle sandboxes."""
    for name in asyncio.run(sandboxes.sweep(idle_minutes)):
        Console().print(f"Deleted {escape(name)}.")


@app.command("viewer")
def open_viewer() -> None:
    """Open the window that shows running sandboxes."""
    viewer.show()


@app.command()
def mcp() -> None:
    """Serve deskhand to Claude Code over MCP (stdio)."""
    mcp_server.main()


@app.command()
def doctor() -> None:
    """Check Claude access, Cua Driver, Docker and the aura."""
    out = Console()
    checks = asyncio.run(run_checks(MODELS[DEFAULT_MODEL]))
    for check in checks:
        mark = "[green]✓[/]" if check.ok else "[red]✗[/]"
        out.print(f"{mark} {check.name}: {escape(check.detail)}")
    if not all(check.ok for check in checks):
        raise typer.Exit(1)
