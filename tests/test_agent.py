import asyncio
import io
import json

import anthropic
import httpx2
import pytest
from conftest import FakeClient, FakeDriver, call, message, note, say
from rich.console import Console

from deskhand.agent import BETAS, Limits, run_task
from deskhand.console import ConsoleView
from deskhand.models import MODELS, ModelAlias
from deskhand.prompts import system_prompt
from deskhand.runlog import RunLog, read_trace
from deskhand.targets.base import Session

DRIVER = ["type_text", "get_desktop_state", "click", "start_recording"]
OPUS = MODELS[ModelAlias.opus]
ROOMY = Limits(max_steps=50, max_cost=2.0)


async def fake_shell(command):
    return f"exit code 0\nran {command}", False


async def run(runs_root, replies, *, limits=ROOMY, shell=True, answers=("a.txt",)):
    log = RunLog(runs_root, "Compute 6 x 7", "sandbox", OPUS.id, "medium")
    view = ConsoleView(Console(file=io.StringIO(), width=200))
    pending = list(answers)

    async def ask(question, *, who="Claude"):
        return pending.pop(0)

    driver = FakeDriver(DRIVER)
    session = Session(name="sandbox", driver=driver, run_shell=fake_shell if shell else None)
    client = FakeClient(replies)
    summary = await run_task(
        task="Compute 6 x 7",
        session=session,
        spec=OPUS,
        effort="medium",
        limits=limits,
        log=log,
        view=view,
        ask=ask,
        client=client,  # type: ignore[arg-type]
    )
    return summary, log, client, driver


async def test_a_finished_task_reports_its_answer(runs_root):
    replies = [
        message(note("Checking the screen"), say("I'll look first."), call("get_desktop_state")),
        message(say("6 × 7 = 42"), stop_reason="end_turn"),
    ]
    summary, log, _, driver = await run(runs_root, replies)

    assert (summary.status, summary.result, summary.steps, summary.tool_calls) == (
        "done",
        "6 × 7 = 42",
        2,
        1,
    )
    # Desktop captures are capped so Claude reads them unscaled (bridge.DEFAULT_ARGUMENTS).
    assert driver.calls == [("get_desktop_state", {"max_image_dimension": 1568})]
    kinds = [e["kind"] for e in read_trace(log.dir)]
    assert kinds == ["start", "turn", "tool", "turn", "end"]
    turn = read_trace(log.dir)[1]
    assert (
        turn["notes"] == ["Checking the screen"]
        and turn["tool_uses"][0]["name"] == "get_desktop_state"
    )
    assert (log.dir / "screens" / "001.png").exists()
    assert json.loads((log.dir / "summary.json").read_text())["status"] == "done"


async def test_request_follows_the_current_api_rules(runs_root):
    _, _, client, _ = await run(runs_root, [message(say("done"), stop_reason="end_turn")])
    params = client.params
    assert params["model"] == "claude-opus-5-5"
    assert params["thinking"] == {"type": "adaptive", "display": "updates"}
    assert params["output_config"] == {"effort": "medium"}
    assert params["cache_control"] == {"type": "ephemeral"}
    assert params["fallbacks"] == "default"
    assert params["betas"] == BETAS
    assert "tool_choice" not in params  # forced tool choice is a 400 on these models
    assert params["system"] == system_prompt("sandbox")
    names = [t["name"] for t in params["tools"]]
    assert names == ["click", "get_desktop_state", "type_text", "ask_user", "shell"]


async def test_history_is_only_appended_to_and_tools_never_change(runs_root):
    replies = [
        message(note("Look"), call("get_desktop_state", "tu_1")),
        message(call("click", "tu_2", x=5, y=6), call("type_text", "tu_3", text="42")),
        message(say("Typed 42"), stop_reason="end_turn"),
    ]
    _, _, client, _ = await run(runs_root, replies)
    first, second, third = client.requests
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert third["messages"][: len(second["messages"])] == second["messages"]
    assert first["tools"] == second["tools"] == third["tools"]
    assert first["system"] == third["system"]
    results = third["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_2", "tu_3"]
    assert all(r["type"] == "tool_result" for r in results)


async def test_tool_failures_go_back_to_claude_as_errors(runs_root):
    replies = [
        message(call("click", "tu_1", element_token="bad"), call("nonexistent", "tu_2")),
        message(say("Gave up"), stop_reason="end_turn"),
    ]
    _, _, client, _ = await run(runs_root, replies)
    results = client.requests[1]["messages"][-1]["content"]
    assert [r.get("is_error") for r in results] == [True, True]
    assert results[1]["content"] == "Unknown tool: nonexistent"


async def test_paused_turn_is_sent_back_without_running_tools(runs_root):
    paused = message(note("still working"), stop_reason="pause_turn")
    replies = [paused, message(say("done"), stop_reason="end_turn")]
    summary, _, client, driver = await run(runs_root, replies)
    assert summary.status == "done" and summary.steps == 2
    assert client.requests[1]["messages"][-1]["role"] == "assistant"
    assert driver.calls == []


async def test_the_mac_gets_no_shell(runs_root):
    _, _, client, _ = await run(
        runs_root, [message(say("done"), stop_reason="end_turn")], shell=False
    )
    assert "shell" not in [t["name"] for t in client.params["tools"]]


async def test_step_limit_stops_the_run(runs_root):
    replies = [message(call("click", f"tu_{i}", x=i, y=i)) for i in range(5)]
    summary, *_ = await run(runs_root, replies, limits=Limits(max_steps=2, max_cost=2.0))
    assert (summary.status, summary.steps) == ("step_limit", 2)


async def test_cost_limit_stops_the_run(runs_root):
    pricey = {"input_tokens": 100_000, "output_tokens": 10_000}  # $0.60 a turn on Opus 5.5
    replies = [message(call("click", f"tu_{i}"), usage=pricey) for i in range(5)]
    summary, *_ = await run(runs_root, replies, limits=Limits(max_steps=50, max_cost=1.0))
    assert (summary.status, summary.steps) == ("cost_limit", 2)
    assert summary.cost_usd == pytest.approx(1.2)


async def test_refusal_is_reported_not_retried(runs_root):
    refusal = message(
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "cyber", "explanation": "Not with this."},
    )
    summary, *_ = await run(
        runs_root, [refusal, message(say("never reached"), stop_reason="end_turn")]
    )
    assert summary.status == "refused" and "Not with this." in summary.error
    assert summary.steps == 1


async def test_cut_off_reply_is_truncated(runs_root):
    summary, *_ = await run(runs_root, [message(say("half"), stop_reason="max_tokens")])
    assert summary.status == "truncated"


async def test_api_errors_end_the_run_with_a_readable_message(runs_root):
    error = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com")
    )
    summary, *_ = await run(runs_root, [error])
    assert summary.status == "error" and "Couldn't reach the Claude API" in summary.error


async def test_ctrl_c_still_writes_the_summary(runs_root):
    with pytest.raises(asyncio.CancelledError):
        await run(runs_root, [message(call("click")), asyncio.CancelledError()])
    (run_dir,) = runs_root.iterdir()
    assert json.loads((run_dir / "summary.json").read_text())["status"] == "interrupted"


async def test_ask_user_and_shell_show_up_like_driver_calls(runs_root):
    replies = [
        message(
            call("ask_user", "tu_1", question="Which file?"), call("shell", "tu_2", command="ls")
        ),
        message(say("Used a.txt"), stop_reason="end_turn"),
    ]
    summary, log, *_ = await run(runs_root, replies)
    tools = [e for e in read_trace(log.dir) if e["kind"] == "tool"]
    assert [(t["name"], t["text"]) for t in tools] == [
        ("ask_user", "a.txt"),
        ("shell", "exit code 0\nran ls"),
    ]
    assert summary.tool_calls == 2
