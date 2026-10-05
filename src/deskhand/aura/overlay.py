"""The aura process: the glow on the target window and deskhand's own cursor.

Runs as its own process (`python -m deskhand.aura`) because AppKit needs the
main thread and its own run loop. Commands arrive one per line on stdin:
`window <id>` puts the glow on that window, `display` on the main display,
`point <x> <y>` and `click <x> <y>` move the cursor to a screen point (Quartz
coordinates; a click also ripples), and `hide` / `show` take everything away and
bring it back (`hide` answers "hidden" once the window server reports it gone).
A timer follows the window's bounds through Quartz, which needs no extra
permission, and EOF on stdin quits, so nothing outlives a run.
"""

# PyObjC builds its AppKit, Quartz and objc names at runtime and ships no type
# stubs, so pyright can't see them; the names are checked when the module runs.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import signal
import sys
import threading
import time
from typing import Any, Literal

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSFloatingWindowLevel,
    NSNormalWindowLevel,
    NSScreen,
    NSWindowAbove,
)
from Foundation import NSEqualRects, NSObject, NSTimer
from PyObjCTools import AppHelper

from deskhand.aura.glow import Glow
from deskhand.aura.pointer import Pointer
from deskhand.aura.screen import make_overlay_window, on_screen_info, window_frame

TICK = 1 / 15  # seconds between bounds checks
HIDE_CONFIRM_S = 0.4  # longest wait for the window server before answering "hidden"


def _point(arg: str) -> tuple[float, float] | None:
    try:
        x, y = (float(part) for part in arg.split())
    except ValueError:
        return None
    return x, y


class Aura(NSObject):
    target: int | Literal["display"] | None

    def init(self) -> Aura | None:
        self = objc.super(Aura, self).init()
        if self is None:
            return None
        self.target = None  # a window id, "display", or None
        self.hidden = False
        self.hide_started = 0.0
        self.window = make_overlay_window()
        self.glow = Glow(self.window.contentView())
        self.pointer = Pointer()
        return self

    def command_(self, line: str) -> None:
        verb, _, arg = line.partition(" ")
        if verb == "window" and arg.isdigit():
            self.target, self.hidden = int(arg), False
        elif verb == "display":
            self.target, self.hidden = "display", False
        elif verb in ("point", "click") and (point := _point(arg)):
            self.pointer.move(*point, click=verb == "click")
        elif verb == "hide":
            self.hidden, self.hide_started = True, time.monotonic()
        elif verb == "show":
            self.hidden = False
        self.tick_(None)
        if verb == "hide":
            self.confirmHidden_(None)

    def confirmHidden_(self, _arg: Any) -> None:
        """Answer "hidden" once the window server has really taken the glow and cursor away.

        orderOut takes effect when the run loop next commits, so this re-checks
        every 10 ms instead of blocking, and gives up waiting after HIDE_CONFIRM_S.
        """
        windows = (self.window, self.pointer.window)
        gone = all(on_screen_info(w.windowNumber()) is None for w in windows)
        if not gone and time.monotonic() - self.hide_started < HIDE_CONFIRM_S:
            self.performSelector_withObject_afterDelay_("confirmHidden:", None, 0.01)
            return
        sys.stdout.write("hidden\n")
        sys.stdout.flush()

    def tick_(self, _timer: Any) -> None:
        if self.hidden or self.target is None:
            self.window.orderOut_(None)
            self.pointer.hide()
            return
        if self.target == "display":
            self.place(NSScreen.screens()[0].frame())
            self.window.setLevel_(NSFloatingWindowLevel)
            self.window.orderFrontRegardless()
            self.pointer.show()
            return
        frame = window_frame(self.target)
        if frame is None:  # closed, minimized or on another Space
            self.window.orderOut_(None)
            self.pointer.hide()
            return
        self.place(frame)
        # Sit just above the target, so windows in front of it still cover the glow.
        self.window.setLevel_(NSNormalWindowLevel)
        self.window.orderWindow_relativeTo_(NSWindowAbove, self.target)
        self.pointer.show()

    @objc.python_method
    def place(self, frame: Any) -> None:
        """Cover `frame` exactly; the glow is drawn inside it."""
        if not NSEqualRects(self.window.frame(), frame):
            self.window.setFrame_display_(frame, True)
        self.glow.layout(frame.size.width, frame.size.height)


def _read_commands(aura: Aura) -> None:
    for line in sys.stdin:
        command = line.strip()
        if command:
            AppHelper.callAfter(aura.command_, command)
    AppHelper.callAfter(AppHelper.stopEventLoop)


def main() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C in the terminal ends it at once
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon, no Cmd-Tab
    aura = Aura.alloc().init()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        TICK, aura, "tick:", None, True
    )
    threading.Thread(target=_read_commands, args=(aura,), daemon=True).start()
    AppHelper.runEventLoop(installInterrupt=False)
