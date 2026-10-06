import io

from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from rich.console import Console

from deskhand import laya_agent
from deskhand.console import ConsoleView
from deskhand.laya_agent import _field, pick_window, run_laya_task, to_page, typed_values
from deskhand.runlog import RunLog, read_trace
from deskhand.snapshot import Snapshot
from deskhand.targets.base import Session

ELEMENTS = [
    {"element_index": 0, "element_token": "s1:0", "role": "AXWindow", "label": "Jobs"},
    {
        "element_index": 1,
        "element_token": "s1:1",
        "role": "AXTextField",
        "label": "Search jobs",
        "value": "",
    },
    {"element_index": 2, "element_token": "s1:2", "role": "AXButton", "label": "Search"},
    {"element_index": 3, "element_token": "s1:3", "role": "AXSecureTextField", "label": "Password"},
    {
        "element_index": 4,
        "element_token": "s1:4",
        "role": "AXCheckBox",
        "label": "Remote",
        "value": "1",
    },
    {
        "element_index": 5,
        "element_token": "s1:5",
        "role": "AXButton",
        "label": "Off",
        "enabled": False,
    },
    {"element_index": 6, "element_token": "s1:6", "role": "push button", "label": "Easy Apply"},
    {"element_index": 7, "element_token": "s1:7", "role": "AXTextField", "label": "Location"},
]
STATE = {
    "app_name": "Google Chrome",
    "window_title": "Jobs | LinkedIn",
    "window_id": 7,
    "pid": 42,
    "elements": ELEMENTS,
    "tree_markdown": '- [0] AXWindow "Jobs"\n  - AXStaticText = "Software Engineer at Acme"\n',
}


def test_typed_values_come_from_the_task_in_order():
    task = (
        "Search for \"software engineer\", then 'remote' jobs at https://example.com/jobs. "
        "Don't stop."
    )
    assert typed_values(task) == ["software engineer", "remote", "https://example.com/jobs"]
    assert typed_values("Open the settings and don't change anything") == []


def test_the_window_the_task_names_wins():
    windows = [
        {"pid": 1, "window_id": 1, "app_name": "Finder", "title": "Downloads", "z_index": 0},
        {"pid": 2, "window_id": 2, "app_name": "Google Chrome", "title": "LinkedIn", "z_index": 5},
        {"pid": 3, "window_id": 3, "app_name": "Dock", "title": "Dock", "z_index": 0},
        {"pid": 4, "window_id": 4, "app_name": "Notes", "title": "", "z_index": 0},
    ]
    picked = pick_window(windows, "In Chrome, apply to jobs")  # one word of "Google Chrome"
    assert picked is not None and picked.window_id == 2
    fallback = pick_window(windows, "do something")
    assert fallback is not None and fallback.window_id == 1  # the frontmost titled window


def test_a_window_becomes_laya_browsers_page():
    snapshot = Snapshot.from_structured(STATE)
    assert snapshot is not None
    page, elements = to_page(snapshot)
    kinds = [(a["id"], a.get("kind"), a.get("role"), a.get("label")) for a in page["actions"]]
    assert kinds[:4] == [
        ("e1", "fill", "textbox", "Search jobs"),
        ("e2", "click", "button", "Search"),
        ("e3", "click", "checkbox", "Remote"),
        ("e4", "click", "button", "Easy Apply"),  # Linux "push button" too
    ]  # the window itself, the password field and the disabled button are left out
    assert page["actions"][2]["checked"] is True
    assert [a["id"] for a in page["actions"][-2:]] == ["scroll_down", "press_enter"]
    assert page["title"] == "Jobs | LinkedIn" and "Software Engineer at Acme" in page["text"]
    assert elements["e4"]["element_token"] == "s1:6"


class Driver:
    def __init__(self):
        self.calls = []

    async def list_tools(self, cursor=None):
        names = ["list_windows", "get_window_state", "click", "type_text", "set_value"]
        schema = {"type": "object", "properties": {"session": {"type": "string"}}}
        return ListToolsResult(tools=[Tool(name=n, input_schema=schema) for n in names])

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        if name == "list_windows":
            windows = [{"pid": 42, "window_id": 7, "app_name": "Google Chrome", "title": "Jobs"}]
            return CallToolResult(
                content=[TextContent(type="text", text="1 window")],
                structured_content={"windows": windows},
            )
        if name == "get_window_state":
            return CallToolResult(
                content=[TextContent(type="text", text="state")], structured_content=STATE
            )
        return CallToolResult(content=[TextContent(type="text", text=f"{name} ok")])


class Brain:
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def decide(self, page, goal, history):
        self.seen.append((page, list(history)))
        operation, action_id = self.script.pop(0)
        action = next((a for a in page["actions"] if a["id"] == action_id), None)
        return {"operation": operation, "action": action, "confidence": 0.9, "ms": 150.0}

    async def aclose(self):
        return None


async def run(
    runs_root, script, task='Search for "software engineer" in Google Chrome', max_steps=10
):
    driver = Driver()
    log = RunLog(runs_root, task, "mac", "laya-browser", "medium")
    view = ConsoleView(Console(file=io.StringIO(), width=200))
    summary = await run_laya_task(
        task=task,
        session=Session(name="mac", driver=driver),
        log=log,
        view=view,
        max_steps=max_steps,
        brain=Brain(script),
    )
    return summary, driver, log


async def test_laya_types_the_tasks_text_clicks_then_finishes(runs_root):
    summary, driver, log = await run(
        runs_root, [("TYPE_TEXT", "e1"), ("CLICK", "e2"), ("DONE", None)]
    )
    acting = [(n, a) for n, a in driver.calls if n in ("type_text", "click")]
    assert acting == [
        (
            "type_text",
            {
                "pid": 42,
                "window_id": 7,
                "element_token": "s1:1",
                "text": "software engineer",
                "delay_ms": 0,  # no wait between keys: 3.1 s instead of 5.1 s in Chrome
            },
        ),
        ("click", {"pid": 42, "window_id": 7, "element_token": "s1:2"}),
    ]
    assert (summary.status, summary.steps, summary.cost_usd) == ("done", 3, 0.0)
    turns = [e for e in read_trace(log.dir) if e["kind"] == "turn"]
    assert turns[0]["notes"][0].startswith("TYPE_TEXT Search jobs (0.90, 150 ms)")


async def test_an_action_that_changes_nothing_stops_the_run(runs_root):
    summary, _, _ = await run(runs_root, [("CLICK", "e2")] * 5)
    assert summary.status == "stuck" and "Search" in summary.error


async def test_typing_with_nothing_to_type_stops_cleanly(runs_root):
    summary, driver, _ = await run(
        runs_root, [("TYPE_TEXT", "e1")], task="Search jobs in Google Chrome"
    )
    assert summary.status == "blocked" and "no text to type" in summary.error
    assert not any(n == "type_text" for n, _ in driver.calls)


async def test_the_step_limit_holds(runs_root):
    summary, _, _ = await run(
        runs_root, [("SCROLL_DOWN", "scroll_down"), ("CLICK", "e2")] * 3, max_steps=2
    )
    assert summary.status == "step_limit" and summary.steps == 2


async def test_a_field_laya_returns_to_gets_its_text_again(runs_root):
    summary, driver, _ = await run(
        runs_root,
        [("TYPE_TEXT", "e1"), ("SCROLL_DOWN", "scroll_down"), ("TYPE_TEXT", "e1"), ("DONE", None)],
    )
    typed = [a["text"] for n, a in driver.calls if n == "type_text"]
    assert typed == ["software engineer", "software engineer"] and summary.status == "done"


def test_in_a_browser_only_the_page_is_offered():
    elements = [
        {"element_index": 0, "element_token": "t:0", "role": "AXWindow", "label": "Search demo"},
        {
            "element_index": 1,
            "element_token": "t:1",
            "role": "AXTextField",
            "label": "Address and search bar",
            "value": "file:///tmp/search.html",
            "parent_index": 0,
        },
        {
            "element_index": 2,
            "element_token": "t:2",
            "role": "AXWebArea",
            "label": "Search demo",
            "parent_index": 0,
        },
        {
            "element_index": 3,
            "element_token": "t:3",
            "role": "AXTextField",
            "label": "Search",
            "parent_index": 2,
        },
        {
            "element_index": 4,
            "element_token": "t:4",
            "role": "AXButton",
            "label": "Search",
            "parent_index": 3,
        },
    ]
    snapshot = Snapshot(app="Firefox", window="Search demo", elements=elements, tree="")
    page, _ = to_page(snapshot)
    labels = [
        (a.get("role"), a.get("label"))
        for a in page["actions"]
        if a.get("kind") in ("fill", "click")
    ]
    assert labels == [("textbox", "Search"), ("button", "Search")]  # not the address bar
    assert page["url"] == "file:///tmp/search.html"  # which still gives the page's URL


def test_the_visible_tabs_page_and_only_its_text():
    tree = (
        '- [0] frame "Search demo — Mozilla Firefox"\n'
        '  - [1] menu bar "Menu Bar"\n'
        '  - [2] document web "New Tab"\n'
        '    - [3] link "Pocket story"\n'
        '  - [4] document web "Search demo"\n'
        '    - [5] entry "Search" value="deskhand"\n'
        '    - [6] paragraph "Results for deskhand"\n'
    )
    elements = [
        {"element_index": 0, "element_token": "f:0", "role": "frame", "label": "Search demo"},
        {
            "element_index": 1,
            "element_token": "f:1",
            "role": "menu bar",
            "label": "Menu Bar",
            "parent_index": 0,
        },
        {
            "element_index": 2,
            "element_token": "f:2",
            "role": "document web",
            "label": "New Tab",
            "parent_index": 0,
        },
        {
            "element_index": 3,
            "element_token": "f:3",
            "role": "link",
            "label": "Pocket story",
            "parent_index": 2,
        },
        {
            "element_index": 4,
            "element_token": "f:4",
            "role": "document web",
            "label": "Search demo",
            "parent_index": 0,
        },
        {
            "element_index": 5,
            "element_token": "f:5",
            "role": "entry",
            "label": "Search",
            "value": "deskhand",
            "parent_index": 4,
        },
    ]
    snapshot = Snapshot(
        app="firefox", window="Search demo — Mozilla Firefox", elements=elements, tree=tree
    )
    page, _ = to_page(snapshot)
    offered = [a["label"] for a in page["actions"] if a.get("kind") in ("fill", "click")]
    assert offered == ["Search"]  # not the background tab's link
    assert page["text"] == "Search demo\nSearch\ndeskhand\nResults for deskhand"


async def test_a_single_value_goes_into_a_second_field_too(runs_root):
    summary, driver, _ = await run(
        runs_root, [("TYPE_TEXT", "e1"), ("TYPE_TEXT", "e5"), ("DONE", None)]
    )  # e5 is the page's second field, "Location"
    typed = [(a["element_token"], a["text"]) for n, a in driver.calls if n == "type_text"]
    assert typed == [("s1:1", "software engineer"), ("s1:7", "software engineer")]
    assert summary.status == "done"


def test_a_field_named_after_its_contents_or_its_placeholder():
    assert _field("Search jobs", "", set()) == ("Search jobs", "")  # a named, empty field
    assert _field("Search jobs", "python", set()) == ("Search jobs", "python")
    # unnamed, holding what the run typed: Cua named it after its contents
    assert _field("software engineer", "software engineer", {"software engineer"}) == (
        "text field",
        "software engineer",
    )
    # empty, with its placeholder reported as name and contents
    assert _field("Describe the job you want", "Describe the job you want", set()) == (
        "Describe the job you want",
        "",
    )


async def test_a_near_guess_ends_the_run_before_it_is_carried_out(runs_root):
    class Unsure(Brain):
        async def decide(self, page, goal, history):
            decision = await super().decide(page, goal, history)
            return {**decision, "confidence": 0.04}

    driver = Driver()
    log = RunLog(runs_root, "t", "mac", "laya-browser", "medium")
    view = ConsoleView(Console(file=io.StringIO(), width=200))
    summary = await run_laya_task(
        task='Search for "x" in Google Chrome',
        session=Session(name="mac", driver=driver),
        log=log,
        view=view,
        max_steps=10,
        brain=Unsure([("CLICK", "e2"), ("SCROLL_DOWN", "scroll_down"), ("CLICK", "e4")]),
    )
    assert summary.status == "unsure" and summary.steps == 1
    assert not any(n == "click" for n, _ in driver.calls)  # the guess wasn't acted on


def test_only_on_screen_elements_within_the_caps(monkeypatch):
    window = {"x": 0, "y": 0, "width": 1000, "height": 800}
    elements = [
        {"element_index": i, "element_token": f"v:{i}", "role": "AXLink", "label": f"Job {i}",
         "frame": {"x": 10, "y": 30 * i, "w": 200, "h": 20}}
        for i in range(1, 80)
    ]  # fmt: skip
    snapshot = Snapshot(app="Chrome", window="Jobs", elements=elements, tree="", bounds=window)
    page, _ = to_page(snapshot)
    links = [a["label"] for a in page["actions"] if a.get("kind") == "click"]
    assert links[0] == "Job 1" and "Job 27" not in links  # below the window's bottom edge
    assert len(links) == 26  # y 30 to 780 are on screen
    monkeypatch.setattr(laya_agent, "MAX_CLICKS", 5)
    page, _ = to_page(snapshot)
    assert len([a for a in page["actions"] if a.get("kind") == "click"]) == 5


async def test_a_broken_driver_ends_the_run_with_an_error(runs_root):
    class Broken(Driver):
        async def call_tool(self, name, arguments=None):
            if name == "click":
                raise ConnectionError("driver went away")
            return await super().call_tool(name, arguments)

    log = RunLog(runs_root, "t", "mac", "laya-browser", "medium")
    view = ConsoleView(Console(file=io.StringIO(), width=200))
    summary = await run_laya_task(
        task='Search for "x" in Google Chrome',
        session=Session(name="mac", driver=Broken()),
        log=log,
        view=view,
        max_steps=10,
        brain=Brain([("CLICK", "e2")]),
    )
    assert summary.status == "error" and "driver went away" in summary.error


async def test_text_already_in_the_field_is_submitted_not_typed_again(runs_root):
    filled = dict(STATE, elements=[dict(e) for e in ELEMENTS])
    filled["elements"][1]["value"] = "software engineer"  # "Search jobs" already holds it

    class Filled(Driver):
        async def call_tool(self, name, arguments=None):
            if name == "get_window_state":
                self.calls.append((name, dict(arguments or {})))
                return CallToolResult(
                    content=[TextContent(type="text", text="state")], structured_content=filled
                )
            return await super().call_tool(name, arguments)

    driver = Filled()
    log = RunLog(runs_root, "t", "mac", "laya-browser", "medium")
    view = ConsoleView(Console(file=io.StringIO(), width=200))
    await run_laya_task(
        task='Search for "software engineer" in Google Chrome',
        session=Session(name="mac", driver=driver),
        log=log,
        view=view,
        max_steps=10,
        brain=Brain([("TYPE_TEXT", "e1"), ("DONE", None)]),
    )
    names = [n for n, _ in driver.calls if n not in ("list_windows", "get_window_state")]
    assert names == ["press_key"]  # no type_text, no second copy of the text
