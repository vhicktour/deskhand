"""AppKit and Quartz helpers shared by the aura's overlay windows.

Overlay windows are borderless, transparent and click-through, kept out of
screen captures, Mission Control and Cmd-Tab, never animated in or out, and
allowed anywhere (AppKit normally keeps windows below the menu bar). Quartz
reports positions with the origin at the top left of the main display and Cocoa
at the bottom left, so y flips against the main display's height; that holds
for every display, since both systems are global.
"""

# PyObjC builds its AppKit, Quartz and objc names at runtime and ships no type
# stubs, so pyright can't see them; the names are checked when the module runs.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from typing import Any

import Quartz
from AppKit import (
    NSBackingStoreBuffered,
    NSColor,
    NSScreen,
    NSView,
    NSWindow,
    NSWindowAnimationBehaviorNone,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowSharingNone,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSMakeRect

ORANGE = (1.0, 0.478, 0.102)  # #FF7A1A


def orange(alpha: float = 1.0) -> Any:
    return Quartz.CGColorCreateSRGB(*ORANGE, alpha)


class OverlayWindow(NSWindow):
    """An overlay window AppKit leaves where it's put.

    By default AppKit keeps a window's top below the menu bar, which would push
    an overlay off any window that reaches the top of the screen.
    """

    def constrainFrameRect_toScreen_(self, frame: Any, screen: Any) -> Any:
        return frame


def make_overlay_window() -> Any:
    window = OverlayWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, 10, 10), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
    )
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setHasShadow_(False)
    window.setIgnoresMouseEvents_(True)
    window.setReleasedWhenClosed_(False)
    window.setAnimationBehavior_(NSWindowAnimationBehaviorNone)  # hide at once, no fade
    window.setSharingType_(NSWindowSharingNone)  # keep it out of screen captures
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorIgnoresCycle
        | NSWindowCollectionBehaviorFullScreenAuxiliary
    )
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
    view.setWantsLayer_(True)
    window.setContentView_(view)
    return window


def cocoa_y(y: float, height: float = 0.0) -> float:
    """A Quartz y (top-left origin) as a Cocoa y for something `height` tall."""
    return NSScreen.screens()[0].frame().size.height - y - height


def on_screen_info(window_id: int) -> Any | None:
    """The window server's entry for a window, or None when it isn't on screen."""
    info = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, window_id)
    if not info or not info[0].get(Quartz.kCGWindowIsOnscreen):
        return None
    return info[0]


def window_frame(window_id: int) -> Any | None:
    """The window's frame in Cocoa screen coordinates, or None when it isn't on screen."""
    entry = on_screen_info(window_id)
    if entry is None:
        return None
    bounds = entry[Quartz.kCGWindowBounds]
    x, y, w, h = bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"]
    return NSMakeRect(x, cocoa_y(y, h), w, h)
