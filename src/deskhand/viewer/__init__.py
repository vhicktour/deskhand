"""The native window that shows deskhand's sandboxes, one live tile each.

show() starts it as a detached process, so it outlives the run or MCP call that
opened it. One viewer runs at a time: it holds viewer.lock for as long as it's
up, and show() starts no other while the lock is taken. When no sandbox is left,
the viewer steps down and closes (see step_down). The window itself is
deskhand.viewer.app, an AppKit program; this package doesn't load AppKit, so the
MCP server and the CLI can call show() cheaply.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TextIO

from deskhand import locks, sandboxes
from deskhand.paths import home, viewer_lock_file

_held: TextIO | None = None  # viewer.lock, open and locked while this process is the viewer


def running() -> bool:
    """Whether a viewer is up. The lock goes when its process does, however it ends."""
    return locks.held(viewer_lock_file())


def show() -> None:
    """Open the viewer window unless it's already open."""
    if running():
        return
    home().mkdir(parents=True, exist_ok=True)
    with (home() / "viewer.log").open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "deskhand.viewer"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # survives the process that opened it
        )


def claim() -> bool:
    """Become the viewer; False when another one is up.

    Two sandboxes made at once both start a viewer; the lock leaves one window.
    """
    global _held
    if _held is None:
        _held = locks.take(viewer_lock_file())
    return _held is not None


def release() -> None:
    """Stop being the viewer. Safe to repeat."""
    global _held
    if _held is not None:
        _held.close()  # closing the file drops the lock
        _held = None


def step_down() -> bool:
    """For a viewer with nothing to show: True when it should close and quit.

    A sandbox made at this moment must still get a window. It's in the registry
    before its show() runs, and show() starts a new viewer once the lock is free.
    So release first, then read the registry once more. A sandbox there keeps
    this viewer if the lock is still free; if a new viewer took the lock in
    between, that one shows it. Either way every sandbox ends up with one window.
    """
    release()
    try:
        empty = not sandboxes.records()
    except (OSError, ValueError):
        empty = False  # can't tell, so keep the window if we can
    return empty or not claim()
