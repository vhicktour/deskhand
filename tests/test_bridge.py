import pytest
from anthropic.lib.tools import ToolError
from conftest import FakeDriver
from mcp.types import ListToolsResult, Tool

from deskhand.bridge import CURATED_TOOLS, driver_tools


class Recorder:
    def __init__(self):
        self.events = []

    async def before_call(self, name, arguments):
        self.events.append(("before", name, arguments))

    async def after_call(self, name, arguments, result, error):
        self.events.append(("after", name, None if result is None else result.is_error, error))


async def test_only_curated_tools_are_offered_sorted_across_pages():
    driver = FakeDriver(
        ["zoom", "start_recording", "click", "kill_app", "get_desktop_state", "set_config"],
        pages=3,
    )
    tools = await driver_tools(driver, [])
    assert [t.name for t in tools] == ["click", "get_desktop_state", "zoom"]


def test_curated_list_leaves_out_plumbing_and_kill_app():
    assert not CURATED_TOOLS & {"kill_app", "start_recording", "set_config", "end_session", "page"}


async def test_hooks_run_before_and_after_each_call():
    driver, hook = FakeDriver(["get_desktop_state"]), Recorder()
    (tool,) = await driver_tools(driver, [hook])
    blocks = await tool.call({"max_image_dimension": 1568})
    assert hook.events == [
        ("before", "get_desktop_state", {"max_image_dimension": 1568}),
        ("after", "get_desktop_state", False, None),
    ]
    assert isinstance(blocks, list)
    assert [b["type"] for b in blocks] == ["image", "text"]
    assert driver.calls == [("get_desktop_state", {"max_image_dimension": 1568})]


async def test_transport_failure_becomes_a_tool_error_hooks_can_see():
    driver, hook = FakeDriver(["click"], fail={"click"}), Recorder()
    (tool,) = await driver_tools(driver, [hook])
    with pytest.raises(ToolError, match="click failed: driver went away"):
        await tool.call({"x": 1, "y": 2})
    assert hook.events[-1] == ("after", "click", None, "click failed: driver went away")


async def test_driver_refusal_reaches_claude_as_an_error():
    driver, hook = FakeDriver(["click"]), Recorder()
    (tool,) = await driver_tools(driver, [hook])
    with pytest.raises(ToolError):
        await tool.call({"element_token": "bad"})
    assert hook.events[-1] == ("after", "click", True, None)


class SessionDriver(FakeDriver):
    """Tools that take a `session` argument, like the real driver's, except get_screen_size."""

    async def list_tools(self, cursor=None):
        listing = await super().list_tools(cursor)
        with_session = {"type": "object", "properties": {"session": {"type": "string"}}}
        tools = [
            Tool(
                name=t.name,
                input_schema=t.input_schema if t.name == "get_screen_size" else with_session,
            )
            for t in listing.tools
        ]
        return ListToolsResult(tools=tools, next_cursor=listing.next_cursor)


async def test_every_call_that_takes_a_session_runs_in_the_targets_one():
    driver = SessionDriver(["click", "get_screen_size"])
    tools = {t.name: t for t in await driver_tools(driver, [], session="deskhand")}
    await tools["click"].call({"x": 1, "y": 2, "session": "picked-by-claude"})
    await tools["get_screen_size"].call({})
    assert driver.calls == [
        ("click", {"x": 1, "y": 2, "session": "deskhand"}),
        ("get_screen_size", {}),
    ]


async def test_desktop_captures_are_capped_unless_claude_sets_a_size():
    driver = FakeDriver(["get_desktop_state"])
    (tool,) = await driver_tools(driver, [])
    await tool.call({})
    await tool.call({"max_image_dimension": 0})
    assert driver.calls == [
        ("get_desktop_state", {"max_image_dimension": 1568}),
        ("get_desktop_state", {"max_image_dimension": 0}),
    ]
