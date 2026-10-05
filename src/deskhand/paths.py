"""Where deskhand keeps its files.

Everything lives under one home: ~/.local/share/deskhand, or DESKHAND_HOME when
set (tests point it at a temporary folder). Runs get a folder each under runs/;
the sandbox registry, the viewer's lock file and Laya's folder (key, lock,
calibration, labelled cases) sit next to it.
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    override = os.environ.get("DESKHAND_HOME")
    return Path(override) if override else Path.home() / ".local" / "share" / "deskhand"


def runs_root() -> Path:
    return home() / "runs"


def sandboxes_file() -> Path:
    return home() / "sandboxes.json"


def viewer_lock_file() -> Path:
    return home() / "viewer.lock"


def laya_dir() -> Path:
    return home() / "laya"
