"""One-at-a-time locks for deskhand's helper processes (the viewer, the Laya server).

A helper holds an flock on its lock file for as long as it runs. The OS drops
the lock when the process ends, however it ends, so a crash or a reboot never
leaves a stale "running" behind the way a pid file can. Asking whether a helper
is up means trying its lock and letting go at once.
"""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import TextIO


def take(path: Path) -> TextIO | None:
    """The lock file, locked by this process, or None when another process has it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def held(path: Path) -> bool:
    """Whether some process holds the lock right now."""
    lock = take(path)
    if lock is None:
        return True
    lock.close()
    return False
