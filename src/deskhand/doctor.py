"""`deskhand doctor`: is everything a run needs in place?

It checks Claude API access with a models lookup (free, and it proves both the
credentials and access to the default model), Cua Driver and its macOS grants
for --on mac, Docker for --on sandbox, and PyObjC for the aura. Each failed
check says how to fix it. Nothing here changes the system.
"""

from __future__ import annotations

import importlib.util
import subprocess
from dataclasses import dataclass

import anthropic
from anthropic import AsyncAnthropic

from deskhand.models import ModelSpec
from deskhand.targets.mac import GRANT_HINT, INSTALL_HINT, find_driver


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _run(args: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


async def check_claude(spec: ModelSpec) -> Check:
    name = "Claude API"
    try:
        model = await AsyncAnthropic().models.retrieve(spec.id)
    except anthropic.AuthenticationError:
        return Check(name, False, "credentials rejected: run `ant auth login`")
    except anthropic.APIStatusError as exc:
        return Check(name, False, f"{exc.status_code}: {exc.message}")
    except Exception as exc:  # no credentials at all, network
        return Check(name, False, f"{exc} (run `ant auth login`)")
    return Check(name, True, f"{model.display_name} ({model.id})")


def check_driver() -> Check:
    name = "Cua Driver (--on mac)"
    binary = find_driver()
    if binary is None:
        return Check(name, False, INSTALL_HINT)
    _, version = _run([binary, "--version"])
    code, output = _run([binary, "check_permissions"])
    if code != 0:
        return Check(
            name, False, f"{output.splitlines()[0] if output else 'check failed'}. {GRANT_HINT}"
        )
    return Check(name, True, f"{version}, permissions granted")


def check_docker() -> Check:
    name = "Docker (--on sandbox)"
    code, output = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if code != 0:
        return Check(name, False, "Docker isn't running: start Docker Desktop")
    return Check(name, True, f"engine {output}")


def check_aura() -> Check:
    name = "Aura (PyObjC)"
    missing = [m for m in ("AppKit", "Quartz") if importlib.util.find_spec(m) is None]
    if missing:
        return Check(name, False, f"missing {', '.join(missing)}: run `uv sync`")
    return Check(name, True, "AppKit and Quartz available")


async def run_checks(spec: ModelSpec) -> list[Check]:
    return [await check_claude(spec), check_driver(), check_docker(), check_aura()]
