import asyncio

import pytest
from mcp.types import CallToolResult

from deskhand.aura import AuraController, ScreenMap, target_window
from deskhand.aura import controller as controller_module


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"target": {"kind": "window", "pid": 1, "window_id": 42}}, 42),
        ({"target": {"kind": "desktop", "display_id": "primary"}}, "display"),
        ({"pid": 1, "window_id": 77}, 77),
        ({"pid": 1, "window_id": "78"}, 78),
        ({"pid": 1}, None),
        ({"window_id": True}, None),
        ({"window_id": "front"}, None),
        ({}, None),
    ],
)
def test_target_window(arguments, expected):
    assert target_window(arguments) == expected


class FakeStdin:
    def __init__(self):
        self.lines = []
        self.closed = False

    def write(self, data):
        self.lines.append(data.decode().strip())

    async def drain(self):
        return None

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


class FakeStdout:
    async def readline(self):
        return b"hidden\n"


class FakeProcess:
    def __init__(self):
        self.stdin = FakeStdin()
        self.stdout = FakeStdout()
        self.returncode = None
        self.started = []

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9


@pytest.fixture
def overlay(monkeypatch):
    proc = FakeProcess()

    async def fake_exec(*args, **kwargs):
        proc.started.append(args)
        return proc

    monkeypatch.setattr(controller_module.asyncio, "create_subprocess_exec", fake_exec)
    return proc


async def test_aura_follows_the_window_and_hides_for_full_screen_captures(overlay):
    async with AuraController() as aura:
        await aura.before_call("get_window_state", {"pid": 1, "window_id": 42})
        await aura.before_call("click", {"target": {"kind": "window", "pid": 1, "window_id": 42}})
        await aura.before_call("get_desktop_state", {})
        await aura.after_call("get_desktop_state", {}, None, None)
        await aura.before_call("type_text", {"pid": 1, "text": "hi"})  # no window named: stay put
        await aura.before_call("click", {"target": {"kind": "desktop", "display_id": "primary"}})
        await aura.before_call("get_window_state", {"pid": 2, "window_id": 99})
    assert overlay.stdin.lines == ["window 42", "hide", "show", "display", "window 99"]
    assert overlay.stdin.closed
    assert overlay.started[0][1:] == ("-m", "deskhand.aura")


async def test_a_dead_overlay_never_breaks_the_run(overlay):
    async with AuraController() as aura:
        overlay.returncode = 1
        await aura.before_call("get_window_state", {"pid": 1, "window_id": 42})
    assert overlay.stdin.lines == []


async def test_hide_waits_for_the_overlay_but_not_forever(overlay, monkeypatch):
    class SilentStdout:
        async def readline(self):
            await asyncio.sleep(10)

    overlay.stdout = SilentStdout()
    monkeypatch.setattr(controller_module, "HIDE_ACK_TIMEOUT_S", 0.01)
    async with AuraController() as aura:
        await aura.before_call("get_desktop_state", {})
    assert overlay.stdin.lines == ["hide"]


# Where calls land: window snapshots from a monitor left of the main display,
# so screen x is negative.
LEFT_WINDOW = {
    "window_id": 7,
    "window_bounds": {"x": -1920.0, "y": -200.0, "width": 1920.0, "height": 1080.0},
    "screenshot_width": 1568,
    "screenshot_height": 882,
    "elements": [
        {"element_token": "s1:4", "frame": {"x": -1800.0, "y": -150.0, "w": 200.0, "h": 40.0}},
    ],
}


def test_screen_map_turns_window_pixels_into_screen_points():
    screen = ScreenMap()
    screen.learn("get_window_state", LEFT_WINDOW)
    centre = screen.locate({"pid": 1, "window_id": 7, "x": 784, "y": 441})  # screenshot centre
    assert centre is not None
    x, y = centre
    assert (round(x), round(y)) == (-960, 340)
    assert screen.locate({"element_token": "s1:4"}) == (-1700.0, -130.0)
    drag = screen.locate({"window_id": 7, "from_x": 0, "from_y": 0, "to_x": 1568, "to_y": 882})
    assert drag == (0.0, 880.0)


def test_screen_map_desktop_pixels_and_unknowns():
    screen = ScreenMap()
    desktop = {"target": {"kind": "desktop", "display_id": "primary"}, "x": 784, "y": 100}
    assert screen.locate(desktop) is None  # no desktop capture yet
    screen.learn(
        "get_desktop_state",
        {
            "screen_width": 2056,
            "screen_height": 1329,
            "screenshot_width": 1568,
            "screenshot_height": 1013,
        },
    )
    spot = screen.locate(desktop)
    assert spot is not None
    x, y = spot
    assert (round(x), round(y)) == (1028, 131)
    assert screen.locate({"window_id": 99, "x": 1, "y": 1}) is None  # never seen
    assert screen.locate({"element_token": "nope"}) is None
    assert screen.locate({"pid": 1, "x": 1, "y": 1}) is None  # no window named


async def test_pointer_follows_clicks_and_typing(overlay):
    async with AuraController() as aura:
        await aura.after_call(
            "get_window_state", {}, CallToolResult(content=[], structured_content=LEFT_WINDOW), None
        )
        await aura.before_call("click", {"pid": 1, "window_id": 7, "x": 784, "y": 441})
        await aura.before_call("type_text", {"pid": 1, "element_token": "s1:4", "text": "hi"})
        await aura.before_call("hotkey", {"pid": 1, "window_id": 7, "keys": ["cmd", "l"]})
    assert overlay.stdin.lines == ["window 7", "click -960.0 340.0", "point -1700.0 -130.0"]
