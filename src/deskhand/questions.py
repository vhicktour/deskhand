"""Questions a run puts to the user when it has no terminal: through its run folder.

A run started from a terminal asks there (ConsoleView.ask). A background run,
the kind task_start launches, has none, so it writes question.json in its run
folder and waits for answer.json: task_status shows the question, Claude asks
the user in Claude Code, and task_answer writes the answer. Each question has an
id, so a late answer can't land on the next question. No answer within the
timeout comes back as an empty string, which the guard reads as a no.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

ANSWER_TIMEOUT_S = 600.0
POLL_S = 1.0


class Ask(Protocol):
    """Ask the user something and wait for the answer; `who` says who is asking."""

    def __call__(self, question: str, *, who: str = "Claude") -> Awaitable[str]: ...


def _write(path: Path, data: dict[str, Any]) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temp, path)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class RunFolderQuestions:
    """An Ask that goes through a run folder, for runs without a terminal."""

    def __init__(self, run_dir: Path, timeout_s: float = ANSWER_TIMEOUT_S) -> None:
        self._question = run_dir / "question.json"
        self._answer = run_dir / "answer.json"
        self._timeout_s = timeout_s

    async def __call__(self, question: str, *, who: str = "Claude") -> str:
        qid = uuid.uuid4().hex[:8]
        self._answer.unlink(missing_ok=True)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        _write(self._question, {"id": qid, "who": who, "question": question, "asked_at": stamp})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_s
        try:
            while loop.time() < deadline:
                reply = _read(self._answer)
                if reply and reply.get("id") == qid:
                    return str(reply.get("answer", ""))
                await asyncio.sleep(POLL_S)
            return ""
        finally:
            self._question.unlink(missing_ok=True)
            self._answer.unlink(missing_ok=True)


def pending(run_dir: Path) -> dict[str, Any] | None:
    """The question a run is waiting on, if any."""
    return _read(run_dir / "question.json")


def answer(run_dir: Path, text: str) -> bool:
    """Answer the run's open question; False when it isn't waiting on one."""
    question = pending(run_dir)
    if question is None:
        return False
    _write(run_dir / "answer.json", {"id": question["id"], "answer": text})
    return True
