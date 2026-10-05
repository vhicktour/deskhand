import io

from rich.console import Console

from deskhand.console import ConsoleView, args_summary
from deskhand.runlog import RunSummary


def test_args_summary_names_the_target_and_key_arguments():
    window = {
        "target": {"kind": "window", "pid": 412, "window_id": 9},
        "element_token": "e_1234567890abcdef",
    }
    assert args_summary(window) == "pid=412 window_id=9 element_token=e_123456789…"
    assert args_summary({"target": {"kind": "desktop"}, "x": 10, "y": 20}) == "desktop x=10 y=20"
    assert args_summary({"pid": 1, "text": "x" * 60}).startswith('pid=1 text="xxxxxxxx')
    assert args_summary({"keys": ["cmd", "c"]}) == 'keys=["cmd", "c"]'


async def test_tool_errors_print_their_first_line():
    out = io.StringIO()
    view = ConsoleView(Console(file=out, width=200))
    await view.before_call("click", {"pid": 1})
    await view.after_call("click", {"pid": 1}, None, "click failed: gone")
    text = out.getvalue()
    assert "→ click" in text and "click failed: gone" in text


def test_finish_shows_status_and_report_path(tmp_path):
    out = io.StringIO()
    view = ConsoleView(Console(file=out, width=200))
    summary = RunSummary(
        id="r", task="t", target="mac", model="m", effort="medium", status="done", result="42"
    )
    view.finish(summary, tmp_path)
    text = out.getvalue()
    assert "42" in text and "done" in text and "report.html" in text
