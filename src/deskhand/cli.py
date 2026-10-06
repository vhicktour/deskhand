"""The deskhand command line.

`run` opens the chosen computer, hands the task to the agent loop and prints a
summary; `runs` and `show` look back at saved runs; `sandbox` manages named
sandboxes; `viewer` opens the sandbox window; `laya` sets up and measures the
guard's local check and `guard` sets whether it asks; `mcp` serves deskhand to
Claude Code; `doctor` checks the setup. Library logs (Anthropic SDK, MCP, cua) go to the
run folder, not the terminal. Exit codes for `run`: 0 when the task finished,
130 after Ctrl+C, 1 for anything else (errors, limits, refusals).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from deskhand.console import ConsoleView
from deskhand.guard import approval, set_approval
from deskhand.laya import server as laya_server
from deskhand.laya.client import Calibration, LayaClient
from deskhand.models import DEFAULT_MODEL, LAYA_ID, MODELS, Limits, ModelAlias, ModelSpec
from deskhand.paths import runs_root
from deskhand.questions import Ask, RunFolderQuestions
from deskhand.runlog import RunLog, RunSummary, find_run, list_runs, load_summary
from deskhand.targets import TargetError

if TYPE_CHECKING:
    from deskhand.laya.evaluate import Row

# Heavy modules (the Claude SDK, the MCP server, cua-sandbox) are imported inside
# the commands that use them: loading them all took 1.4 s before every command,
# and a Laya run on the Mac needs none of them.

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Give Claude a task; it does it on your Mac or in a Linux sandbox.",
)
sandbox_app = typer.Typer(no_args_is_help=True, help="Named Linux sandboxes (at most 3 at once).")
app.add_typer(sandbox_app, name="sandbox")
laya_app = typer.Typer(no_args_is_help=True, help="Laya, the fast local check behind the guard.")
app.add_typer(laya_app, name="laya")


@app.callback()
def _quiet_libraries() -> None:
    # Importing cua_sandbox installs an INFO-level log handler on the root logger,
    # which printed library chatter (every HTTP request) under each command. Every
    # command starts from warnings only; `run` then sends its logs to the run folder.
    logging.basicConfig(level=logging.WARNING, force=True)


class On(StrEnum):
    sandbox = "sandbox"
    mac = "mac"


class Approval(StrEnum):
    ask = "ask"
    allow = "allow"


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
    guard: bool,
    log: RunLog,
    console: ConsoleView,
) -> RunSummary:
    ask: Ask = console.ask if sys.stdin.isatty() else RunFolderQuestions(log.dir)
    laya = LayaClient(Calibration.load()) if guard else None
    gate = None
    if laya is not None:
        try:
            laya_server.start()  # loads while Claude takes its first turn
            from deskhand.guard import Guard

            setting = approval()
            gate = Guard(client=laya, ask=ask, log=log, warn=console.info, approval=setting)
            if setting == "allow":
                console.info("The guard is on, set to allow: it checks and records, never asks.")
            else:
                console.info("The guard is on: Laya checks each action, and it asks you first.")
        except laya_server.LayaUnavailable as exc:
            console.info(f"The guard is off for this run: {exc}")
    if on is On.mac:
        from deskhand.targets.mac import open_mac

        target = open_mac(aura=aura, log_dir=log.dir)
    else:
        from deskhand.targets.sandbox import open_sandbox

        target = open_sandbox(name=sandbox, keep=keep, view=view, info=console.info)
    try:
        async with target as session:
            if spec.id == LAYA_ID:  # no Claude: laya-browser picks every action
                from deskhand.laya_agent import run_laya_task

                return await run_laya_task(
                    task=task,
                    session=session,
                    log=log,
                    view=console,
                    max_steps=limits.max_steps,
                    gate=gate,
                )
            from deskhand.agent import run_task

            return await run_task(
                task=task,
                session=session,
                spec=spec,
                effort=effort.value,
                limits=limits,
                log=log,
                view=console,
                ask=ask,
                gate=gate,
            )
    except TargetError as exc:
        return log.finish(status="error", error=str(exc))
    finally:
        if laya is not None:
            await laya.aclose()


def _guard_on(flag: bool | None, on: On) -> bool:
    """--guard/--no-guard when given; otherwise on for the Mac once the eval has passed."""
    if flag is not None:
        return flag
    return on is On.mac and laya_server.installed() and Calibration.load().passed


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="What to do, in plain English.")],
    on: Annotated[
        On, typer.Option(help="The computer: a local Linux sandbox, or this Mac.")
    ] = On.sandbox,
    model: Annotated[
        ModelAlias,
        typer.Option(
            help="opus: Claude Opus 5.5 · sonnet: Sonnet 5.5 · fable: Fable 5.1 · "
            "laya: no Claude, the local laya-browser model picks every action (free)."
        ),
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
    guard: Annotated[
        bool | None,
        typer.Option(
            "--guard/--no-guard",
            help="Have Laya check each action and ask you before anything hard to undo "
            "(default: on for the Mac once `deskhand laya eval` has passed).",
        ),
    ] = None,
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
                guard=_guard_on(guard, on),
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
    from deskhand import sandboxes, viewer

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
    from deskhand import sandboxes

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
    from deskhand import sandboxes

    asyncio.run(sandboxes.delete(name))
    Console().print(f"Deleted {escape(name)}.")


@sandbox_app.command("url")
def sandbox_url(
    name: Annotated[str, typer.Argument(help="A name from `deskhand sandbox list`.")],
) -> None:
    """Print a fresh viewer link for a sandbox (links last an hour)."""
    from deskhand import sandboxes

    Console().print(asyncio.run(sandboxes.viewer_url(name)), soft_wrap=True)


@sandbox_app.command("sweep")
def sandbox_sweep(
    idle_minutes: Annotated[
        int | None,
        typer.Option(min=0, help="Delete sandboxes idle this long (default: the 30-minute sweep)."),
    ] = None,
) -> None:
    """Delete idle sandboxes."""
    from deskhand import sandboxes

    minutes = sandboxes.IDLE_MINUTES if idle_minutes is None else idle_minutes
    for name in asyncio.run(sandboxes.sweep(minutes)):
        Console().print(f"Deleted {escape(name)}.")


@app.command("viewer")
def open_viewer() -> None:
    """Open the window that shows running sandboxes."""
    from deskhand import viewer

    viewer.show()


@app.command()
def mcp() -> None:
    """Serve deskhand to Claude Code over MCP (stdio)."""
    from deskhand import mcp_server

    mcp_server.main()


@app.command()
def doctor() -> None:
    """Check Claude access, Cua Driver, Docker and the aura."""
    out = Console()
    from deskhand.doctor import run_checks

    checks = asyncio.run(run_checks(MODELS[DEFAULT_MODEL]))
    for check in checks:
        mark = "[green]✓[/]" if check.ok else "[red]✗[/]"
        out.print(f"{mark} {check.name}: {escape(check.detail)}")
    if not all(check.ok for check in checks):
        raise typer.Exit(1)


@app.command("guard")
def guard_approval(
    setting: Annotated[
        Approval | None,
        typer.Argument(help="ask: ask you before a flagged action. allow: never ask, just record."),
    ] = None,
) -> None:
    """Show or set what the guard does with a flagged action."""
    if setting is not None:
        set_approval(setting.value)
    current = approval()
    meaning = (
        "it asks you before an action Laya flags"
        if current == "ask"
        else "it never asks; it records what it would have asked and still warns Claude about "
        "text aimed at it"
    )
    Console().print(f"Guard approval: {current} ({meaning}).")


@laya_app.command("setup")
def laya_setup() -> None:
    """Install Laya and its model (about 800 MB) in their own environment, then start it."""
    out = Console()
    try:
        report = laya_server.setup(lambda text: out.print(escape(text)))
    except (laya_server.LayaUnavailable, subprocess.CalledProcessError) as exc:
        out.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from exc
    out.print(
        f"Laya is up on {report.get('device', 'an unknown device')}. Next: deskhand laya eval"
    )


@laya_app.command("status")
def laya_status() -> None:
    """Whether Laya is set up and running, and how its last measurement went."""
    out = Console()
    if not laya_server.installed():
        out.print("Laya isn't installed. Run: deskhand laya setup")
        return
    report = laya_server.health() if laya_server.running() else None
    where = (
        f"up on {report.get('device', '?')}" if report else "stopped (starts when a check needs it)"
    )
    out.print(f"Guard server: {where}")
    brain = laya_server.running(laya_server.BRAIN)
    out.print(f"laya-browser (--model laya): {'up' if brain else 'stopped (starts with a run)'}")
    calibration = Calibration.load()
    if not calibration.fits:
        out.print("Not measured yet. Run: deskhand laya eval")
        return
    for name, fit in calibration.fits.items():
        verdict = "passes" if fit.passed else "below the bar"
        out.print(
            f"{name}: {verdict} · catches {fit.recall:.0%} with {fit.false_alarms:.0%} false "
            f"alarms · AUROC {fit.auroc:.2f}"
        )
    default = "on" if calibration.passed else "off"
    out.print(f"Guard on Mac runs by default: {default} (measured {calibration.measured_at})")


@laya_app.command("stop")
def laya_stop() -> None:
    """Stop the Laya servers now (they also stop by themselves after 30 idle minutes)."""
    out = Console()
    for service in laya_server.SERVICES.values():
        stopped = laya_server.stop(service)
        out.print(f"{service.title}: {'stopping' if stopped else 'not running'}")


@laya_app.command("serve", hidden=True)
def laya_serve(service: Annotated[str, typer.Option()] = laya_server.GUARD.name) -> None:
    laya_server.serve(laya_server.SERVICES[service])


@laya_app.command("cases")
def laya_cases(
    label: Annotated[bool, typer.Option(help="Have Claude label the new cases.")] = True,
    model: Annotated[
        ModelAlias, typer.Option(help="The Claude model that labels.")
    ] = DEFAULT_MODEL,
) -> None:
    """Collect guard cases from past runs and have Claude label the new ones."""
    from anthropic import AsyncAnthropic

    from deskhand.laya import cases

    out = Console()
    rows, added = cases.collect(runs_root())
    todo = sum(row.get("label") is None for row in rows)
    cost = 0.0
    if label and todo:
        cost = asyncio.run(cases.label(rows, AsyncAnthropic(), MODELS[model]))
    cases.save(rows)
    labelled = todo if label else 0
    out.print(f"{added} new case(s), {len(rows)} in all; labelled {labelled} for ${cost:.2f}.")
    for name in sorted({row["question"] for row in rows}):
        yes = sum(row["question"] == name and row.get("label") is True for row in rows)
        no = sum(row["question"] == name and row.get("label") is False for row in rows)
        out.print(f"  {name}: {yes} yes, {no} no")
    out.print(f"[dim]Review or fix labels in {cases.run_cases_file()}[/]")


async def _measure_all(client: LayaClient) -> tuple[Calibration, list[Row]]:
    from deskhand.laya import cases
    from deskhand.laya.evaluate import measure

    try:
        return await measure(cases.synthetic_cases() + cases.labelled_run_cases(), client)
    finally:
        await client.aclose()


@laya_app.command("eval")
def laya_eval() -> None:
    """Measure Laya on the labelled cases and fit the guard's thresholds."""
    from deskhand.laya.evaluate import BAR_FALSE_ALARMS, BAR_RECALL

    out = Console()
    try:
        calibration, rows = asyncio.run(_measure_all(LayaClient(timeout_s=60)))
    except laya_server.LayaUnavailable as exc:
        out.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from exc
    calibration.save()
    table = Table(
        "Question", "Yes/no", "AUROC", "Caught", "False alarms", "Threshold", "Calib. error",
        "ms (p50/p95)", box=None, pad_edge=False,
    )  # fmt: skip
    for row in rows:
        fit = row.fit
        table.add_row(
            row.question,
            f"{fit.positives}/{fit.negatives}",
            f"{fit.auroc:.2f}",
            f"{fit.recall:.0%}",
            f"[{'green' if fit.passed else 'red'}]{fit.false_alarms:.0%}[/]",
            f"{fit.threshold:.2f}",
            f"{row.ece_before:.2f}→{row.ece_after:.2f}",
            f"{row.ms_median:.0f}/{row.ms_p95:.0f}",
        )
    out.print(table)
    bar = f"catch {BAR_RECALL:.0%} with at most {BAR_FALSE_ALARMS:.0%} false alarms"
    if calibration.passed:
        out.print(f"Both questions {bar}: the guard is now on by default for Mac runs.")
    else:
        out.print(
            f"Below the bar ({bar}), so the guard stays off unless you pass --guard. "
            "Fine-tuning Laya on labelled cases is the next step."
        )
