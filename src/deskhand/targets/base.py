"""What an open target gives the agent loop.

Each target is an async context manager that sets up a computer and yields a
Session: an MCP client connected to Cua Driver on that computer, any hooks the
target adds to driver calls (the aura on the Mac), the driver session every
call should run in (when the target manages one), the live viewer link if
there is one, and a shell runner where running commands is safe (the sandbox
only). Leaving the context tears down whatever the target set up.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from deskhand.bridge import CallHook, DriverClient
from deskhand.prompts import TargetName

# Runs one command; returns the text Claude sees and whether it failed.
ShellRunner = Callable[[str], Awaitable[tuple[str, bool]]]


class TargetError(Exception):
    """A setup problem the user can fix; the message says how."""


@dataclass
class Session:
    name: TargetName
    driver: DriverClient
    hooks: list[CallHook] = field(default_factory=list)
    driver_session: str | None = None
    viewer_url: str | None = None
    run_shell: ShellRunner | None = None
