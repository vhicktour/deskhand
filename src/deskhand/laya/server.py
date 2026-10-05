"""The shared local Laya server: install it, start it on demand, stop it when idle.

laya-serve runs from its own uv tool environment. ensure() starts it through a
small launcher process (`deskhand laya serve`) that holds a lock while it lives,
so only one server runs, and that stops it after IDLE_STOP_MINUTES without a
check (Laya itself only unloads the model when idle). laya-serve's defaults are
every network interface and no key, so it gets 127.0.0.1 and a random key kept
in Laya's folder, readable by this user only.
"""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx2

from deskhand import locks
from deskhand.paths import laya_dir

PORT = 8790
INSTALL_SPEC = "laya[serve]"
INSTALL_PYTHON = "3.12"
IDLE_STOP_MINUTES = 30
IDLE_UNLOAD_S = 600
WATCH_S = 15
START_TIMEOUT_S = 120.0
SETUP_TIMEOUT_S = 1800.0  # the first start downloads the model, about 800 MB

# A request shape laya-serve hasn't seen yet compiles GPU kernels first, which
# took up to 10 seconds on an M1. One warm-up per typical state length keeps
# the guard's 3-second limit for real checks.
_WARM_UP_STATES = ("Click.", 'In Mail, window "Inbox": click on button "Reply". ' * 4, "x " * 300)


class LayaUnavailable(Exception):
    """Laya isn't installed, didn't start, or didn't answer in time."""


def _lock_file() -> Path:
    return laya_dir() / "server.lock"


def _pid_file() -> Path:
    return laya_dir() / "server.pid"


def _last_used_file() -> Path:
    return laya_dir() / "last_used"


def url() -> str:
    return f"http://127.0.0.1:{PORT}"


def installed() -> bool:
    return shutil.which("laya-serve") is not None


def key() -> str:
    """The key laya-serve requires, made on first use and readable by this user only."""
    path = laya_dir() / "key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    return path.read_text().strip()


def headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {key()}"}


def server_env(base: Mapping[str, str]) -> dict[str, str]:
    """laya-serve's settings: this Mac only, the English checkpoint, a key."""
    return {
        **base,
        "LAYA_HOST": "127.0.0.1",
        "LAYA_PORT": str(PORT),
        "LAYA_MODELS": "english",
        "LAYA_PRELOAD": "1",
        "LAYA_IDLE_UNLOAD_SECONDS": str(IDLE_UNLOAD_S),
        "LAYA_API_KEY": key(),
        "LAYA_LOG_LEVEL": "warning",
        "USE_TF": "0",  # Laya's card: a TensorFlow probe can hang model loading
    }


def running() -> bool:
    return locks.held(_lock_file())


def touch() -> None:
    """Mark Laya as just used, which keeps the launcher from stopping it."""
    path = _last_used_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def idle_seconds() -> float:
    try:
        return time.time() - _last_used_file().stat().st_mtime
    except OSError:
        return float("inf")


def health(timeout_s: float = 2.0) -> dict[str, Any] | None:
    try:
        reply = httpx2.get(f"{url()}/health", headers=headers(), timeout=timeout_s)
        return reply.json() if reply.status_code == 200 else None
    except (httpx2.HTTPError, ValueError):
        return None


def start() -> None:
    """Start the launcher in the background unless a server is already up."""
    if running():
        return
    if not installed():
        raise LayaUnavailable("Laya isn't installed. Run: deskhand laya setup")
    laya_dir().mkdir(parents=True, exist_ok=True)
    with (laya_dir() / "server.log").open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "deskhand", "laya", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # outlives the run that started it
        )


def ensure(timeout_s: float = START_TIMEOUT_S) -> dict[str, Any]:
    """Start Laya if needed and wait until it answers; its /health report."""
    start()
    touch()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        report = health()
        if report and report.get("status") == "ok":
            _warm_up(deadline - time.monotonic())
            return report
        time.sleep(0.5)
    raise LayaUnavailable(
        f"Laya didn't start within {timeout_s:.0f} s; see {laya_dir()}/server.log"
    )


def _warm_up(timeout_s: float) -> None:
    question = {"type": "choice", "instructions": "Warm-up.", "criteria": {"A": "yes", "B": "no"}}
    for state in _WARM_UP_STATES:
        try:
            httpx2.post(
                f"{url()}/v1/systemone",
                headers=headers(),
                json={"state": state, "questions": {"q": question}},
                timeout=max(timeout_s, 1.0),
            )
        except httpx2.HTTPError:
            return  # the server is up; a slow first check fails open like any other


def stop() -> bool:
    """Stop the server; False when none is running."""
    if not running():
        return False
    try:
        os.kill(int(_pid_file().read_text()), signal.SIGTERM)
    except (OSError, ValueError):
        return False
    return True


def serve() -> None:
    """The launcher: run laya-serve until it has been idle IDLE_STOP_MINUTES."""
    lock = locks.take(_lock_file())
    if lock is None:
        return  # another launcher has the server
    program = shutil.which("laya-serve")
    if program is None:
        raise LayaUnavailable("Laya isn't installed. Run: deskhand laya setup")
    _pid_file().write_text(str(os.getpid()))
    touch()

    def leave(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, leave)
    signal.signal(signal.SIGINT, leave)
    child = subprocess.Popen([program], env=server_env(os.environ), stdin=subprocess.DEVNULL)
    try:
        while child.poll() is None and idle_seconds() < IDLE_STOP_MINUTES * 60:
            time.sleep(WATCH_S)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        _pid_file().unlink(missing_ok=True)
        lock.close()


def setup(say: Callable[[str], None]) -> dict[str, Any]:
    """Install Laya with uv if needed, then start it once, which downloads the model."""
    if not installed():
        uv = shutil.which("uv")
        if uv is None:
            raise LayaUnavailable("uv isn't on PATH; install it from https://docs.astral.sh/uv/")
        say(f"Installing {INSTALL_SPEC} (PyTorch included, a few minutes)…")
        subprocess.run(
            [uv, "tool", "install", "--python", INSTALL_PYTHON, INSTALL_SPEC], check=True
        )
    say("Starting Laya (the first start downloads the model, about 800 MB)…")
    return ensure(SETUP_TIMEOUT_S)
