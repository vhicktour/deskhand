"""Fakes shared by the unit tests: Cua Driver over MCP, a scripted Claude, cua sandboxes.

FakeDriver answers list_tools and call_tool like the real driver, returning a
text block and, for screenshot tools, a small PNG. FakeClient stands in for
AsyncAnthropic's streaming call and replays scripted replies; the real loop runs
the tool calls in them, so the bridge, hooks, run log and console are exercised
together without a network. FakeSandboxClass stands in for cua's Sandbox class.
Every test gets its own DESKHAND_HOME, so none touches real runs or sandboxes.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage
from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool

PNG_1PX = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    )
).decode()

SCREENSHOT_TOOLS = {"get_desktop_state", "get_window_state", "zoom"}


def tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"{name} (fake)",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
    )


class FakeDriver:
    """A Cua Driver MCP client: fixed tool list, scripted results, a record of calls."""

    def __init__(self, names: Sequence[str], pages: int = 1, fail: set[str] | None = None) -> None:
        self.names = list(names)
        self.pages = pages
        self.fail = fail or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self, cursor: str | None = None) -> ListToolsResult:
        size = -(-len(self.names) // self.pages)
        start = int(cursor or 0)
        chunk = self.names[start : start + size]
        nxt = str(start + size) if start + size < len(self.names) else None
        return ListToolsResult(tools=[tool(n) for n in chunk], next_cursor=nxt)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        self.calls.append((name, dict(arguments or {})))
        if name in self.fail:
            raise ConnectionError("driver went away")
        content: list[Any] = [TextContent(type="text", text=f"{name} ok")]
        if name in SCREENSHOT_TOOLS:
            content.insert(0, ImageContent(type="image", data=PNG_1PX, mime_type="image/png"))
        return CallToolResult(content=content, is_error=name == "click" and "bad" in str(arguments))


def message(
    *blocks: dict[str, Any],
    stop_reason: str = "tool_use",
    usage: dict[str, int] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": list(blocks),
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "stop_details": stop_details,
            "usage": usage or {"input_tokens": 1000, "output_tokens": 100},
        }
    )


def note(text: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": "sig"}


def say(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def call(name: str, tool_id: str = "tu_1", **arguments: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}


class _Stream:
    def __init__(self, reply: BetaMessage | BaseException) -> None:
        self._reply = reply

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_final_message(self) -> BetaMessage:
        if isinstance(self._reply, BaseException):
            raise self._reply
        return self._reply


class FakeClient:
    """AsyncAnthropic with just enough surface: client.beta.messages.stream(...).

    Replays the scripted replies in order and keeps every request, with a copy of
    its messages as they were when it was sent.
    """

    def __init__(self, replies: list[BetaMessage | BaseException]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    @property
    def params(self) -> dict[str, Any]:
        return self.requests[0]

    def stream(self, **params: Any) -> _Stream:
        self.requests.append({**params, "messages": list(params["messages"])})
        return _Stream(self.replies.pop(0))


@pytest.fixture(autouse=True)
def deskhand_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKHAND_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def runs_root(deskhand_home):
    return deskhand_home / "runs"


class FakeSandboxClass:
    """Stands in for cua_sandbox.Sandbox: an in-memory set of named sandboxes."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.commands: list[tuple[str, str]] = []

    async def list(self, local: bool = True) -> list[Any]:
        others = [SimpleNamespace(name="other")]  # cua sandboxes deskhand didn't make
        return [SimpleNamespace(name=n) for n in sorted(self.names)] + others

    async def create(self, image: Any, local: bool = True, name: str = "") -> Any:
        self.names.add(name)
        return _Handle(self, name)

    async def delete(self, name: str, local: bool = True) -> None:
        self.names.discard(name)

    def connect(self, name: str, local: bool = True) -> Any:
        if name not in self.names:
            raise RuntimeError(f"no sandbox {name}")
        return _Handle(self, name)


class _Handle:
    def __init__(self, owner: FakeSandboxClass, name: str) -> None:
        self._owner, self.name = owner, name
        self.shell = SimpleNamespace(run=self._run)

    async def _run(self, command: str, **options: Any) -> Any:  # options: cua's timeout
        self._owner.commands.append((self.name, command))
        return SimpleNamespace(returncode=0, stdout=f"ran {command}", stderr="")

    async def viewer_url(self) -> str:
        return f"http://127.0.0.1:1/viewer/#{self.name}"

    async def screenshot(self) -> bytes:
        return b"\x89PNG fake"

    async def disconnect(self) -> None:
        return None

    async def __aenter__(self) -> _Handle:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def cua(monkeypatch):
    from deskhand import sandboxes

    fake = FakeSandboxClass()
    monkeypatch.setattr(sandboxes, "Sandbox", fake)
    return fake
