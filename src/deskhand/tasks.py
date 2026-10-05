"""Task runs in the background, for the MCP server.

start() launches `python -m deskhand run …` as its own process with a run id
chosen up front, so a run survives an MCP server restart and picks up code
changes; its console output goes to console.log in the run folder and its pid
to a pid file. status() reads the run folder: summary.json once the run has
ended, the trace while it's going, and a pending question ("waiting") that
answer() replies to. stop() sends Ctrl+C, so the run still writes its summary
and deletes a sandbox it made.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from deskhand import questions
from deskhand.paths import runs_root
from deskhand.runlog import load_summary, new_run_id, read_trace

POLL_S = 2.0
MAX_WAIT_S = 600
NOTES_SHOWN = 4

# Runs this process started, so finished ones are reaped and never read as alive.
_children: dict[str, subprocess.Popen[bytes]] = {}


@dataclass
class TaskStatus:
    run_id: str
    status: str
    steps: int = 0
    cost_usd: float = 0.0
    result: str = ""
    error: str = ""
    latest: list[str] = field(default_factory=list)
    report: str = ""
    question: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def start(
    task: str,
    *,
    on: str,
    sandbox: str | None,
    model: str,
    effort: str,
    max_steps: int,
    max_cost: float,
    guard: bool | None = None,
) -> str:
    """Start a run in the background and return its id."""
    root = runs_root()
    run_id = new_run_id(root, task)
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    args = [
        sys.executable, "-m", "deskhand", "run", task,
        "--on", on, "--model", model, "--effort", effort,
        "--max-steps", str(max_steps), "--max-cost", str(max_cost),
        "--run-id", run_id,
    ]  # fmt: skip
    if sandbox:
        args += ["--sandbox", sandbox]
    if guard is not None:
        args.append("--guard" if guard else "--no-guard")
    with (run_dir / "console.log").open("ab") as console:
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=console, stderr=console, start_new_session=True
        )
    (run_dir / "pid").write_text(str(proc.pid))
    _children[run_id] = proc
    return run_id


def _alive(run_id: str) -> bool:
    proc = _children.get(run_id)
    if proc is not None:
        return proc.poll() is None
    try:
        os.kill(int((runs_root() / run_id / "pid").read_text()), 0)
    except (OSError, ValueError):
        return False
    return True


def status(run_id: str) -> TaskStatus:
    run_dir = runs_root() / run_id
    if not run_dir.is_dir():
        return TaskStatus(run_id, "unknown", error=f"No run {run_id}.")
    report = str(run_dir / "report.html")
    if (run_dir / "summary.json").exists():
        s = load_summary(run_dir)
        return TaskStatus(run_id, s.status, s.steps, s.cost_usd, s.result, s.error, [], report)
    turns = [e for e in read_trace(run_dir) if e["kind"] == "turn"]
    latest = [t for e in turns[-2:] for t in [*e.get("notes", []), *e.get("text", [])]]
    cost = round(sum(e.get("cost", 0.0) for e in turns), 4)
    if _alive(run_id):
        asked = questions.pending(run_dir)
        if asked is not None:
            question = f"{asked.get('who', 'Claude')} asks: {asked.get('question', '')}"
            return TaskStatus(
                run_id, "waiting", len(turns), cost, latest=latest[-NOTES_SHOWN:], question=question
            )
        return TaskStatus(run_id, "running", len(turns), cost, latest=latest[-NOTES_SHOWN:])
    console = run_dir / "console.log"
    tail = console.read_text(errors="replace")[-1500:] if console.exists() else ""
    return TaskStatus(run_id, "crashed", len(turns), cost, error=tail, report=report)


async def wait(run_id: str, wait_s: float) -> TaskStatus:
    """status(), after waiting up to wait_s seconds for the run to end."""
    deadline = asyncio.get_running_loop().time() + min(max(wait_s, 0.0), MAX_WAIT_S)
    current = status(run_id)
    while current.status == "running" and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(POLL_S)
        current = status(run_id)
    return current


def stop(run_id: str) -> bool:
    """Ctrl+C the run; False when it isn't running."""
    if not _alive(run_id):
        return False
    try:
        os.kill(int((runs_root() / run_id / "pid").read_text()), signal.SIGINT)
    except (OSError, ValueError):
        return False
    return True


def answer(run_id: str, text: str) -> bool:
    """Answer the question a run is waiting on; False when it isn't waiting."""
    run_dir = runs_root() / run_id
    return run_dir.is_dir() and _alive(run_id) and questions.answer(run_dir, text)
