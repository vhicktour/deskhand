import json

from conftest import PNG_1PX
from mcp.types import CallToolResult, ImageContent, TextContent

from deskhand.runlog import RunLog, find_run, list_runs, load_summary, read_trace


def new_log(root, task="Open <Calculator> & compute"):
    return RunLog(root, task, "mac", "claude-opus-5-5", "medium")


async def test_driver_calls_land_in_the_trace_with_their_screenshots(runs_root):
    log = new_log(runs_root)
    shot = CallToolResult(
        content=[
            ImageContent(type="image", data=PNG_1PX, mime_type="image/png"),
            TextContent(type="text", text="Desktop 1280x800"),
        ]
    )
    await log.after_call("get_desktop_state", {}, shot, None)
    await log.after_call("click", {"x": 1}, None, "click failed: gone")

    events = read_trace(log.dir)
    assert [e["kind"] for e in events] == ["start", "tool", "tool"]
    assert events[1]["images"] == ["screens/001.png"]
    assert events[1]["text"] == "Desktop 1280x800"
    assert events[2]["is_error"] and events[2]["text"] == "click failed: gone"
    assert (log.dir / "screens" / "001.png").read_bytes()[:4] == b"\x89PNG"
    assert log.summary.tool_calls == 2


def test_finish_writes_summary_and_escaped_report_once(runs_root):
    log = new_log(runs_root)
    log.finish(status="done", result="<b>42</b>", steps=3, cost_usd=0.12)
    log.finish(status="error", error="too late")  # ignored

    summary = json.loads((log.dir / "summary.json").read_text())
    assert summary["status"] == "done" and summary["result"] == "<b>42</b>"
    report = (log.dir / "report.html").read_text()
    assert "&lt;b&gt;42&lt;/b&gt;" in report and "<b>42</b>" not in report
    assert "Open &lt;Calculator&gt; &amp; compute" in report
    assert [e["kind"] for e in read_trace(log.dir)].count("end") == 1


def test_runs_are_listed_newest_first_and_found_by_prefix(runs_root):
    first = new_log(runs_root, "same task")
    second = new_log(runs_root, "same task")  # same second, same slug
    assert first.dir != second.dir
    first.finish(status="done")

    listed = list_runs(runs_root)
    assert [s.id for s in listed] == sorted([first.dir.name, second.dir.name], reverse=True)
    assert load_summary(second.dir).status == "unfinished"
    assert find_run(runs_root, first.dir.name) == first.dir
    assert find_run(runs_root, first.dir.name[:8]) is None  # ambiguous prefix
    assert find_run(runs_root, "nope") is None


def test_no_runs_yet(runs_root):
    assert list_runs(runs_root) == []
