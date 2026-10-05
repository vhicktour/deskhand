"""deskhand · sandboxes: a window with one live tile per running sandbox.

Each tile is a WebKit view of the sandbox's own HTML5 viewer (video, input,
clipboard), so you can watch an agent work and step in. Every two seconds the
window re-reads the sandbox registry, so tiles come and go with the sandboxes.
When none has been left for CLOSE_AFTER_S, the window closes itself, which quits
the app just as closing it by hand does. A viewer link older than RENEW_AFTER_MIN
is renewed (links expire after an hour) by `deskhand sandbox url NAME`, run in
the background, which saves the new one to the registry.
"""

# PyObjC builds its AppKit, WebKit and objc names at runtime and ships no type
# stubs, so pyright can't see them; the names are checked when the module runs.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import atexit
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Any

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSColor,
    NSFont,
    NSTextAlignmentCenter,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSURL, NSMakeRect, NSObject, NSTimer, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKAudiovisualMediaTypeNone, WKWebView, WKWebViewConfiguration

from deskhand import sandboxes
from deskhand.viewer import claim, release, step_down

REFRESH_S = 2.0
CLOSE_AFTER_S = 10.0  # long enough for an agent to delete one sandbox and make the next
RENEW_AFTER_MIN = 45
RENEW_RETRY_S = 120
LABEL_H = 22.0
GAP = 6.0


def _label(text: str, size: float = 12.0) -> Any:
    field = NSTextField.labelWithString_(text)
    field.setFont_(NSFont.systemFontOfSize_(size))
    field.setTextColor_(NSColor.secondaryLabelColor())
    return field


class Tile:
    """One sandbox: its name and owner above a live view of its desktop."""

    def __init__(self, parent: Any, record: sandboxes.SandboxRecord) -> None:
        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 100, 100))
        self.label = _label(f"{record.name} · {record.owner}")
        config = WKWebViewConfiguration.alloc().init()
        config.setMediaTypesRequiringUserActionForPlayback_(WKAudiovisualMediaTypeNone)
        self.web = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, 100, 100), config
        )
        self.view.addSubview_(self.label)
        self.view.addSubview_(self.web)
        parent.addSubview_(self.view)
        self.url = ""
        self.load(record.viewer_url)

    def load(self, url: str) -> None:
        if url and url != self.url:
            self.url = url
            self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))

    def place(self, x: float, y: float, width: float, height: float) -> None:
        self.view.setFrame_(NSMakeRect(x, y, width, height))
        self.label.setFrame_(NSMakeRect(4, height - LABEL_H, width - 8, LABEL_H))
        self.web.setFrame_(NSMakeRect(0, 0, width, height - LABEL_H))

    def remove(self) -> None:
        self.view.removeFromSuperview()


class Viewer(NSObject):
    def init(self) -> Viewer | None:
        self = objc.super(Viewer, self).init()
        if self is None:
            return None
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskResizable
            | NSWindowStyleMaskMiniaturizable
        )
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 1100, 720), style, NSBackingStoreBuffered, False
        )
        self.window.setTitle_("deskhand · sandboxes")
        self.window.setReleasedWhenClosed_(False)
        self.window.setDelegate_(self)
        self.window.center()
        self.empty = _label("No sandboxes running. This window closes on its own.", 14)
        self.empty.setAlignment_(NSTextAlignmentCenter)
        self.window.contentView().addSubview_(self.empty)
        self.tiles: dict[str, Tile] = {}
        self.renewing: dict[str, float] = {}
        self.empty_since: float | None = None
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            REFRESH_S, self, "refresh:", None, True
        )
        return self

    def refresh_(self, _timer: Any) -> None:
        try:
            records = {r.name: r for r in sandboxes.records()}
        except (OSError, ValueError):
            return  # registry mid-write or unreadable: try again next tick
        for name in [n for n in self.tiles if n not in records]:
            self.tiles.pop(name).remove()
        for name, record in records.items():
            if name in self.tiles:
                self.tiles[name].load(record.viewer_url)
            else:
                self.tiles[name] = Tile(self.window.contentView(), record)
            self.renew_if_old(record)
        self.layout()
        self.close_if_idle(showing=bool(records))

    @objc.python_method
    def close_if_idle(self, *, showing: bool) -> None:
        """Close the window once there has been nothing to show for CLOSE_AFTER_S."""
        if showing:
            self.empty_since = None
        elif self.empty_since is None:
            self.empty_since = time.monotonic()
        elif time.monotonic() - self.empty_since >= CLOSE_AFTER_S and step_down():
            self.window.close()  # windowWillClose_ quits the app

    @objc.python_method
    def renew_if_old(self, record: sandboxes.SandboxRecord) -> None:
        stamp = record.viewer_url_at
        age = datetime.now().astimezone() - datetime.fromisoformat(stamp) if stamp else None
        if age is not None and age < timedelta(minutes=RENEW_AFTER_MIN):
            return
        if time.monotonic() - self.renewing.get(record.name, float("-inf")) < RENEW_RETRY_S:
            return
        self.renewing[record.name] = time.monotonic()
        subprocess.Popen(
            [sys.executable, "-m", "deskhand", "sandbox", "url", record.name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @objc.python_method
    def layout(self) -> None:
        bounds = self.window.contentView().bounds()
        width, height = bounds.size.width, bounds.size.height
        self.empty.setHidden_(bool(self.tiles))
        self.empty.setFrame_(NSMakeRect(0, height / 2 - 12, width, 24))
        tiles = [self.tiles[name] for name in sorted(self.tiles)]
        if not tiles:
            return
        cols = 1 if len(tiles) == 1 else 2
        rows = -(-len(tiles) // cols)
        tile_w = (width - GAP * (cols + 1)) / cols
        tile_h = (height - GAP * (rows + 1)) / rows
        for i, tile in enumerate(tiles):
            row, col = divmod(i, cols)
            x = GAP + col * (tile_w + GAP)
            y = height - GAP - (row + 1) * tile_h - row * GAP
            tile.place(x, y, tile_w, tile_h)

    def windowDidResize_(self, _note: Any) -> None:
        self.layout()

    def windowWillClose_(self, _note: Any) -> None:
        self.timer.invalidate()
        AppHelper.stopEventLoop()


def main() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if not claim():
        return  # another viewer already has the window
    atexit.register(release)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon
    viewer = Viewer.alloc().init()
    viewer.refresh_(None)
    viewer.window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    AppHelper.runEventLoop(installInterrupt=False)
