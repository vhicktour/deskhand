"""Everything a run leaves behind, in one folder per run.

~/.local/share/deskhand/runs/<YYYYMMDD-HHMMSS>-<slug>/ holds trace.jsonl (one
event per line, appended as the run goes, so a crash still leaves a trace),
screens/NNN.png (every image Cua Driver returned), summary.json (written when
the run ends, however it ends) and report.html. A run started by the MCP server
gets its id up front, and its folder already holds console.log by then.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult

from deskhand import report

_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def new_run_id(root: Path, task: str) -> str:
    """A fresh run id: start time plus a slug of the task, unique under root."""
    base = f"{datetime.now():%Y%m%d-%H%M%S}-{_slug(task)}"
    run_id, n = base, 2
    while (root / run_id).exists():  # same task started twice in one second
        run_id, n = f"{base}-{n}", n + 1
    return run_id


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _slug(text: str, limit: int = 40) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return words[:limit].rstrip("-") or "task"


@dataclass
class RunSummary:
    id: str
    task: str
    target: str
    model: str
    effort: str
    status: str = "running"
    result: str = ""
    error: str = ""
    steps: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    started_at: str = field(default_factory=_now)
    ended_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunSummary:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class RunLog:
    """The folder of one run. Also a CallHook, so it sees every driver call."""

    def __init__(
        self,
        root: Path,
        task: str,
        target: str,
        model: str,
        effort: str,
        run_id: str | None = None,
    ) -> None:
        run_id = run_id or new_run_id(root, task)
        self.dir = root / run_id
        self.screens = self.dir / "screens"
        self.screens.mkdir(parents=True, exist_ok=True)
        self.summary = RunSummary(id=run_id, task=task, target=target, model=model, effort=effort)
        self._images = 0
        self._finished = False
        self.event("start", task=task, target=target, model=model, effort=effort)

    def event(self, kind: str, **data: Any) -> None:
        line = json.dumps({"time": _now(), "kind": kind, **data}, ensure_ascii=False, default=str)
        with (self.dir / "trace.jsonl").open("a", encoding="utf-8") as trace:
            trace.write(line + "\n")

    def save_image(self, data_b64: str, mime_type: str) -> str:
        """Write one image to screens/ and return its path relative to the run folder."""
        self._images += 1
        name = f"{self._images:03d}.{_EXTENSIONS.get(mime_type, 'img')}"
        (self.screens / name).write_bytes(base64.b64decode(data_b64))
        return f"screens/{name}"

    def record_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        text: str,
        is_error: bool,
        images: list[str] | None = None,
    ) -> None:
        """One tool call and its outcome, from the driver or from deskhand's own tools."""
        self.summary.tool_calls += 1
        self.event(
            "tool",
            name=name,
            arguments=arguments,
            is_error=is_error,
            text=text,
            images=images or [],
        )

    async def before_call(self, name: str, arguments: dict[str, Any]) -> None:
        return None

    async def after_call(
        self,
        name: str,
        arguments: dict[str, Any],
        result: CallToolResult | None,
        error: str | None,
    ) -> None:
        if result is None:
            self.record_tool(name, arguments, error or "", is_error=True)
            return
        texts: list[str] = []
        images: list[str] = []
        for block in result.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "image":
                images.append(self.save_image(block.data, block.mime_type))
        self.record_tool(name, arguments, "\n".join(texts), bool(result.is_error), images)

    def finish(self, **updates: Any) -> RunSummary:
        """Write summary.json and report.html. Only the first call counts."""
        if self._finished:
            return self.summary
        self._finished = True
        for key, value in updates.items():
            setattr(self.summary, key, value)
        self.summary.ended_at = _now()
        self.event(
            "end", status=self.summary.status, result=self.summary.result, error=self.summary.error
        )
        (self.dir / "summary.json").write_text(
            json.dumps(asdict(self.summary), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        report.write(self.dir, self.summary, read_trace(self.dir))
        return self.summary


def read_trace(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "trace.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as trace:
        return [json.loads(line) for line in trace if line.strip()]


def load_summary(run_dir: Path) -> RunSummary:
    """A run's summary; a run that never finished is rebuilt from its trace."""
    path = run_dir / "summary.json"
    if path.exists():
        return RunSummary.from_dict(json.loads(path.read_text(encoding="utf-8")))
    events = read_trace(run_dir)
    start = next((e for e in events if e["kind"] == "start"), {})
    return RunSummary(
        id=run_dir.name,
        task=start.get("task", ""),
        target=start.get("target", ""),
        model=start.get("model", ""),
        effort=start.get("effort", ""),
        status="unfinished",
        started_at=start.get("time", ""),
    )


def list_runs(root: Path) -> list[RunSummary]:
    """Every run under root, newest first."""
    if not root.exists():
        return []
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
    return [load_summary(d) for d in dirs]


def find_run(root: Path, run_id: str) -> Path | None:
    """The run folder whose name is run_id or starts with it (a unique prefix)."""
    exact = root / run_id
    if exact.is_dir():
        return exact
    matches = [d for d in root.glob(f"{run_id}*") if d.is_dir()] if root.exists() else []
    return matches[0] if len(matches) == 1 else None
