"""End to end in a real Linux sandbox with the real Claude API.

Skipped by default (pyproject sets `-m 'not live'`); run with `pytest -m live`.
It needs Docker Desktop running and Claude credentials, takes about 30 seconds
once the image is cached, and spends model tokens (about $0.20 with Opus 5.5).
It checks the pixel path on purpose: over a stateless MCP connection every pixel
action failed, so the task starts with a click by position.
"""

import io
import subprocess

import pytest
from rich.console import Console

from deskhand.agent import Limits, run_task
from deskhand.console import ConsoleView
from deskhand.models import DEFAULT_MODEL, MODELS
from deskhand.runlog import RunLog, read_trace
from deskhand.targets.sandbox import open_sandbox

pytestmark = pytest.mark.live

TASK = (
    "Open a terminal window on the desktop. Click inside it by position (x and y from its "
    "screenshot), then type the command echo deskhand-$((6*7)) and press Enter. Read the "
    "output from the terminal window and reply with just that output."
)


def sandbox_containers() -> set[str]:
    names = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True
    ).stdout.split()
    return {n for n in names if n.startswith("deskhand-")}


async def test_a_gui_task_runs_end_to_end(runs_root, tmp_path, monkeypatch):
    # cua refuses to write the real ~/.cua from a test; Docker keeps the image cached.
    monkeypatch.setenv("CUA_HOME", str(tmp_path / "cua"))
    before = sandbox_containers()
    spec = MODELS[DEFAULT_MODEL]
    log = RunLog(runs_root, TASK, "sandbox", spec.id, "medium")
    view = ConsoleView(Console(file=io.StringIO()))
    async with open_sandbox(name=None, keep=False, view=False, info=print) as session:
        summary = await run_task(
            task=TASK,
            session=session,
            spec=spec,
            effort="medium",
            limits=Limits(max_steps=25, max_cost=1.50),
            log=log,
            view=view,
        )

    assert summary.status == "done", summary.error
    assert "deskhand-42" in summary.result
    tools = [e for e in read_trace(log.dir) if e["kind"] == "tool"]
    assert any(t["name"] == "click" and "x" in t["arguments"] for t in tools), "no pixel click"
    assert not [t for t in tools if "No current snapshot" in t["text"]], "driver lost its session"
    assert {"type_text", "press_key", "hotkey"} & {t["name"] for t in tools}
    assert any((log.dir / "screens").iterdir())
    assert sandbox_containers() <= before, "the sandbox was not deleted"
