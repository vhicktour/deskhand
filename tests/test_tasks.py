import json
import signal

from deskhand import tasks
from deskhand.paths import runs_root
from deskhand.runlog import RunLog


class FakeProc:
    def __init__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.pid = 4242
        self.code = None

    def poll(self):
        return self.code


def start(monkeypatch, **overrides):
    launched = []

    def popen(args, **kwargs):
        launched.append(FakeProc(args, **kwargs))
        return launched[-1]

    monkeypatch.setattr(tasks.subprocess, "Popen", popen)
    options = {
        "on": "sandbox",
        "sandbox": None,
        "model": "opus",
        "effort": "medium",
        "max_steps": 9,
        "max_cost": 1.5,
    }
    run_id = tasks.start("Open the calculator", **{**options, **overrides})
    return run_id, launched[0]


def test_start_launches_a_detached_run_with_its_id(monkeypatch):
    run_id, proc = start(monkeypatch, sandbox="deskhand-a-1")
    args = proc.args
    assert args[1:4] == ["-m", "deskhand", "run"] and args[4] == "Open the calculator"
    assert args[args.index("--run-id") + 1] == run_id
    assert args[args.index("--sandbox") + 1] == "deskhand-a-1"
    assert proc.kwargs["start_new_session"] is True
    run_dir = runs_root() / run_id
    assert (run_dir / "pid").read_text() == "4242" and (run_dir / "console.log").exists()


def test_status_while_running_then_when_done(monkeypatch):
    run_id, proc = start(monkeypatch)
    log = RunLog(
        runs_root(), "Open the calculator", "sandbox", "claude-opus-5-5", "medium", run_id=run_id
    )
    log.event("turn", step=1, notes=["Opening it"], text=[], cost=0.12)
    running = tasks.status(run_id)
    assert (running.status, running.steps, running.cost_usd, running.latest) == (
        "running",
        1,
        0.12,
        ["Opening it"],
    )

    log.finish(status="done", result="42", steps=1, cost_usd=0.12)
    proc.code = 0
    done = tasks.status(run_id)
    assert (done.status, done.result) == ("done", "42")
    assert done.report.endswith(f"{run_id}/report.html")


def test_a_run_that_died_without_a_summary_is_crashed(monkeypatch):
    run_id, proc = start(monkeypatch)
    (runs_root() / run_id / "console.log").write_text("Traceback: boom")
    proc.code = 1
    crashed = tasks.status(run_id)
    assert crashed.status == "crashed" and "boom" in crashed.error


async def test_wait_returns_once_the_run_ends(monkeypatch):
    run_id, proc = start(monkeypatch)
    monkeypatch.setattr(tasks, "POLL_S", 0.01)
    calls = {"n": 0}
    real_status = tasks.status
    summary = {
        "id": run_id,
        "task": "t",
        "target": "sandbox",
        "model": "m",
        "effort": "medium",
        "status": "done",
        "result": "ok",
    }

    def finishing(rid):
        calls["n"] += 1
        if calls["n"] == 3:
            proc.code = 0
            (runs_root() / rid / "summary.json").write_text(json.dumps(summary))
        return real_status(rid)

    monkeypatch.setattr(tasks, "status", finishing)
    result = await tasks.wait(run_id, 5)
    assert result.status == "done" and calls["n"] == 3


def test_stop_sends_ctrl_c_only_to_a_live_run(monkeypatch):
    run_id, proc = start(monkeypatch)
    sent = []
    monkeypatch.setattr(tasks.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert tasks.stop(run_id) is True and sent == [(4242, signal.SIGINT)]
    proc.code = 0
    assert tasks.stop(run_id) is False


def test_unknown_run():
    assert tasks.status("nope").status == "unknown"


def test_start_passes_the_guard_choice(monkeypatch):
    assert start(monkeypatch, guard=True)[1].args[-1] == "--guard"
    assert start(monkeypatch, guard=False)[1].args[-1] == "--no-guard"
    args = start(monkeypatch)[1].args
    assert "--guard" not in args and "--no-guard" not in args


def test_a_run_with_a_question_is_waiting_until_answered(monkeypatch):
    run_id, _ = start(monkeypatch)
    run_dir = runs_root() / run_id
    question = {"id": "q1", "who": "deskhand's guard", "question": "Allow it?"}
    (run_dir / "question.json").write_text(json.dumps(question))
    waiting = tasks.status(run_id)
    assert (waiting.status, waiting.question) == ("waiting", "deskhand's guard asks: Allow it?")
    assert tasks.answer(run_id, "no")
    assert json.loads((run_dir / "answer.json").read_text()) == {"id": "q1", "answer": "no"}
    (run_dir / "question.json").unlink()
    assert tasks.status(run_id).status == "running"
    assert not tasks.answer(run_id, "late") and not tasks.answer("no-such-run", "x")
