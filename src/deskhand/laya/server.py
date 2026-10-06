"""Local Laya servers: install them, start them on demand, stop them when idle.

Two run from Laya's own uv tool environment, each its own Service: GUARD is
laya-serve with the English checkpoint, which the guard asks; BRAIN is
policy_server.py with laya-browser, which picks every action of a
`--model laya` run. ensure() starts one through a small launcher process
(`deskhand laya serve`) that holds a lock while it lives, so only one of each
runs, and that stops it after a while without a request (30 minutes for
GUARD, 2 hours for BRAIN, whose reload cost a run 13 s). Both listen
on 127.0.0.1 only and want a random key kept in Laya's folder (mode 600).
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from deskhand import locks
from deskhand.paths import laya_dir

INSTALL_SPEC = "laya[serve]"
INSTALL_PYTHON = "3.12"
IDLE_STOP_MINUTES = 30
IDLE_UNLOAD_S = 600
WATCH_S = 15
START_TIMEOUT_S = 120.0
SETUP_TIMEOUT_S = 1800.0  # a first start downloads its model, 650 to 800 MB

# A request shape laya-serve hasn't seen yet compiles GPU kernels first, which
# took up to 10 seconds on an M1. One warm-up per typical state length keeps
# the guard's 3-second limit for real checks. (BRAIN warms itself up.)
_WARM_UP_STATES = ("Click.", 'In Mail, window "Inbox": click on button "Reply". ' * 4, "x " * 300)


class LayaUnavailable(Exception):
    """Laya isn't installed, didn't start, or didn't answer in time."""


@dataclass(frozen=True)
class Service:
    """One local server; its files in Laya's folder are named after it."""

    name: str
    port: int
    title: str
    health_path: str
    idle_stop_minutes: int = IDLE_STOP_MINUTES

    def file(self, suffix: str) -> Path:
        return laya_dir() / f"{self.name}.{suffix}"


GUARD = Service("server", 8790, "Laya", "/health")
# laya-browser stays up longer: reloading it made a run's first step wait 13 s.
BRAIN = Service("brain", 8791, "laya-browser", "/", idle_stop_minutes=120)
SERVICES = {s.name: s for s in (GUARD, BRAIN)}


def url(service: Service = GUARD) -> str:
    return f"http://127.0.0.1:{service.port}"


def installed() -> bool:
    return shutil.which("laya-serve") is not None


def laya_python() -> str:
    """The Python of Laya's uv tool environment, from laya-serve's first line."""
    program = shutil.which("laya-serve")
    if program is None:
        raise LayaUnavailable("Laya isn't installed. Run: deskhand laya setup")
    first = Path(program).read_text(errors="replace").splitlines()[0]
    return first.removeprefix("#!").strip()


def key() -> str:
    """The key both servers require, made on first use and readable by this user only."""
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
        "LAYA_PORT": str(GUARD.port),
        "LAYA_MODELS": "english",
        "LAYA_PRELOAD": "1",
        "LAYA_IDLE_UNLOAD_SECONDS": str(IDLE_UNLOAD_S),
        "LAYA_API_KEY": key(),
        "LAYA_LOG_LEVEL": "warning",
        "USE_TF": "0",  # Laya's card: a TensorFlow probe can hang model loading
    }


def _command(service: Service) -> tuple[list[str], dict[str, str]]:
    if service is GUARD:
        program = shutil.which("laya-serve")
        if program is None:
            raise LayaUnavailable("Laya isn't installed. Run: deskhand laya setup")
        return [program], server_env(os.environ)
    script = Path(__file__).with_name("policy_server.py")
    env = {**os.environ, "DESKHAND_LAYA_KEY": key(), "USE_TF": "0"}
    return [laya_python(), str(script), str(service.port)], env


def running(service: Service = GUARD) -> bool:
    return locks.held(service.file("lock"))


def touch(service: Service = GUARD) -> None:
    """Mark a server as just used, which keeps its launcher from stopping it."""
    path = service.file("last_used")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def idle_seconds(service: Service = GUARD) -> float:
    try:
        return time.time() - service.file("last_used").stat().st_mtime
    except OSError:
        return float("inf")


def health(timeout_s: float = 2.0, service: Service = GUARD) -> dict[str, Any] | None:
    try:
        reply = httpx2.get(
            f"{url(service)}{service.health_path}", headers=headers(), timeout=timeout_s
        )
        return reply.json() if reply.status_code == 200 else None
    except (httpx2.HTTPError, ValueError):
        return None


def start(service: Service = GUARD) -> None:
    """Start the service's launcher in the background unless it's already up."""
    if running(service):
        return
    if not installed():
        raise LayaUnavailable("Laya isn't installed. Run: deskhand laya setup")
    laya_dir().mkdir(parents=True, exist_ok=True)
    with service.file("log").open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "deskhand", "laya", "serve", "--service", service.name],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # outlives the run that started it
        )


def ensure(timeout_s: float = START_TIMEOUT_S, service: Service = GUARD) -> dict[str, Any]:
    """Start a server if needed and wait until it answers; its health report."""
    start(service)
    touch(service)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        report = health(service=service)
        if report and report.get("status") == "ok":
            if service is GUARD:
                _warm_up(deadline - time.monotonic())
            return report
        time.sleep(0.5)
    log = service.file("log")
    raise LayaUnavailable(f"{service.title} didn't start within {timeout_s:.0f} s; see {log}")


def _warm_up(timeout_s: float) -> None:
    question = {"type": "choice", "instructions": "Warm-up.", "criteria": {"A": "yes", "B": "no"}}
    for state in _WARM_UP_STATES:
        try:
            httpx2.post(
                f"{url(GUARD)}/v1/systemone",
                headers=headers(),
                json={"state": state, "questions": {"q": question}},
                timeout=max(timeout_s, 1.0),
            )
        except httpx2.HTTPError:
            return  # the server is up; a slow first check fails open like any other


def stop(service: Service = GUARD) -> bool:
    """Stop a server; False when it isn't running."""
    if not running(service):
        return False
    try:
        os.kill(int(service.file("pid").read_text()), signal.SIGTERM)
    except (OSError, ValueError):
        return False
    return True


def serve(service: Service = GUARD) -> None:
    """The launcher: run the server until it has been idle its idle_stop_minutes."""
    lock = locks.take(service.file("lock"))
    if lock is None:
        return  # another launcher has this server
    command, env = _command(service)
    service.file("pid").write_text(str(os.getpid()))
    touch(service)

    def leave(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, leave)
    signal.signal(signal.SIGINT, leave)
    child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL)
    try:
        while child.poll() is None and idle_seconds(service) < service.idle_stop_minutes * 60:
            time.sleep(WATCH_S)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        service.file("pid").unlink(missing_ok=True)
        lock.close()


def setup(say: Callable[[str], None]) -> dict[str, Any]:
    """Install Laya with uv if needed, then start both servers once (downloads the models)."""
    if not installed():
        uv = shutil.which("uv")
        if uv is None:
            raise LayaUnavailable("uv isn't on PATH; install it from https://docs.astral.sh/uv/")
        say(f"Installing {INSTALL_SPEC} (PyTorch included, a few minutes)…")
        subprocess.run(
            [uv, "tool", "install", "--python", INSTALL_PYTHON, INSTALL_SPEC], check=True
        )
    say("Starting Laya (the first start downloads its model, about 800 MB)…")
    report = ensure(SETUP_TIMEOUT_S, GUARD)
    say("Starting laya-browser, which drives `--model laya` runs (about 650 MB)…")
    ensure(SETUP_TIMEOUT_S, BRAIN)
    return report
