"""Where a driver call lands on screen, worked out from the driver's own snapshots.

A call names its spot in one of three ways: an element_token from a window
snapshot (whose frame the snapshot gives in screen points), x/y in a window
screenshot's pixels (scaled by the screenshot's size against the window's
bounds), or x/y in the full-display capture's pixels for desktop-wide actions.
ScreenMap remembers the geometry each snapshot reports and turns any of these
into a screen point in Quartz coordinates (top-left origin, all displays), or
None when it can't know.
"""

from __future__ import annotations

from typing import Any, Literal

Where = int | Literal["display"]
Point = tuple[float, float]


def target_window(arguments: dict[str, Any]) -> Where | None:
    """The window a driver call acts on, "display" for screen-wide input, else None."""
    target = arguments.get("target")
    if isinstance(target, dict):
        if target.get("kind") == "desktop":
            return "display"
        window_id = target.get("window_id")
    else:
        window_id = arguments.get("window_id")
    if isinstance(window_id, bool):
        return None
    if isinstance(window_id, int):
        return window_id
    if isinstance(window_id, str) and window_id.isdigit():
        return int(window_id)
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class ScreenMap:
    def __init__(self) -> None:
        # window id -> (x, y, width, height) in screen points, and screenshot size in pixels
        self._bounds: dict[int, tuple[float, float, float, float]] = {}
        self._shots: dict[int, tuple[float, float]] = {}
        self._elements: dict[int, dict[str, Point]] = {}  # window id -> token -> centre
        self._desktop: tuple[float, float] | None = None  # screen points per capture pixel

    def learn(self, tool: str, structured: Any) -> None:
        """Keep the geometry a get_window_state or get_desktop_state result reports."""
        if not isinstance(structured, dict):
            return
        if tool == "get_window_state":
            self._learn_window(structured)
        elif tool == "get_desktop_state":
            points_w = _number(structured.get("screen_width"))
            points_h = _number(structured.get("screen_height"))
            pixels_w = _number(structured.get("screenshot_width"))
            pixels_h = _number(structured.get("screenshot_height"))
            if points_w and points_h and pixels_w and pixels_h:
                self._desktop = (points_w / pixels_w, points_h / pixels_h)

    def _learn_window(self, state: dict[str, Any]) -> None:
        window_id = state.get("window_id")
        bounds = state.get("window_bounds")
        if not isinstance(window_id, int) or not isinstance(bounds, dict):
            return
        self._bounds[window_id] = (
            float(bounds["x"]),
            float(bounds["y"]),
            float(bounds["width"]),
            float(bounds["height"]),
        )
        width, height = state.get("screenshot_width"), state.get("screenshot_height")
        if isinstance(width, int | float) and isinstance(height, int | float) and width and height:
            self._shots[window_id] = (float(width), float(height))
        centres: dict[str, Point] = {}
        for element in state.get("elements") or []:
            token, frame = element.get("element_token"), element.get("frame")
            if isinstance(token, str) and isinstance(frame, dict):
                centres[token] = (
                    float(frame["x"]) + float(frame["w"]) / 2,
                    float(frame["y"]) + float(frame["h"]) / 2,
                )
        if centres:
            self._elements[window_id] = centres

    def locate(self, arguments: dict[str, Any]) -> Point | None:
        """The screen point a call acts on, if the snapshots so far tell us."""
        token = arguments.get("element_token")
        if isinstance(token, str):
            return next((c[token] for c in self._elements.values() if token in c), None)
        x = _number(arguments.get("to_x", arguments.get("x")))  # a drag ends at to_x, to_y
        y = _number(arguments.get("to_y", arguments.get("y")))
        if x is None or y is None:
            return None
        where = target_window(arguments)
        if where == "display":
            if self._desktop is None:
                return None
            return x * self._desktop[0], y * self._desktop[1]
        if where is None or where not in self._bounds or where not in self._shots:
            return None
        left, top, width, height = self._bounds[where]
        shot_w, shot_h = self._shots[where]
        return left + x * width / shot_w, top + y * height / shot_h
