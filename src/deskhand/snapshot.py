"""Reading Cua Driver's window snapshots: the element a call targets, and the text.

Next to its screenshot, get_window_state returns a structured snapshot: the app
and window title, the actionable elements (token, role, label, frame in
screenshot pixels) and the accessibility tree as markdown, where an element's
own words often sit on child lines (a sidebar cell holds a static text
"Recents"). These helpers find the element a call names, by token or by
position, describe it in a few words, and pull the visible text out of the tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_INDEX = re.compile(r"\[(\d+)\]")
_TREE_LINE = re.compile(r"^\s*- (\[\d+\] |AX)", re.MULTILINE)
TEXT_CHUNK_CHARS = 1000


def role_words(role: str) -> str:
    """AXPopUpButton -> "pop up button"."""
    name = role.removeprefix("AX")
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name).lower() or "element"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


@dataclass
class Snapshot:
    app: str = ""
    window: str = ""
    window_id: int | None = None
    pid: int | None = None
    elements: list[dict[str, Any]] = field(default_factory=list)
    tree: str = ""

    @classmethod
    def from_structured(cls, data: dict[str, Any] | None) -> Snapshot | None:
        if not data or "elements" not in data:
            return None
        return cls(
            app=str(data.get("app_name") or ""),
            window=str(data.get("window_title") or ""),
            window_id=data["window_id"] if isinstance(data.get("window_id"), int) else None,
            pid=data["pid"] if isinstance(data.get("pid"), int) else None,
            elements=list(data.get("elements") or []),
            tree=str(data.get("tree_markdown") or ""),
        )

    def by_token(self, token: str) -> dict[str, Any] | None:
        return next((e for e in self.elements if e.get("element_token") == token), None)

    def at(self, x: float, y: float) -> dict[str, Any] | None:
        """The smallest element whose screenshot frame holds the point."""
        hits = []
        for element in self.elements:
            frame = element.get("screenshot_frame") or {}
            fx, fy, fw, fh = (frame.get(k, 0) for k in ("x", "y", "w", "h"))
            if fw and fh and fx <= x <= fx + fw and fy <= y <= fy + fh:
                hits.append((fw * fh, element))
        return min(hits, key=lambda hit: hit[0])[1] if hits else None

    def _words_under(self, index: int) -> list[str]:
        """Quoted words on the element's own tree line and the lines nested under it."""
        lines = self.tree.splitlines()
        for i, line in enumerate(lines):
            match = _INDEX.search(line)
            if match and int(match.group(1)) == index:
                words = _QUOTED.findall(line)
                for child in lines[i + 1 :]:
                    if _indent(child) <= _indent(line) or _INDEX.search(child):
                        break
                    words += _QUOTED.findall(child)
                return words
        return []

    def describe(self, element: dict[str, Any]) -> str:
        """An element in a few words: its role and what it says, e.g. button "Send"."""
        role = role_words(str(element.get("role", "")))
        label = str(element.get("label") or "")
        words = [label] if label else self._words_under(int(element.get("element_index", -1)))
        text = " ".join(w for w in words if w)[:120]
        return f'{role} "{text}"' if text else role

    def text(self) -> str:
        """The words a person could read in the window, one tree entry per line."""
        return visible_text(self.tree)


def visible_text(tree_or_text: str) -> str:
    """The quoted words of an accessibility tree, or plain text as it is.

    A tree's markup ("AXWindow ... actions=[raise]") is never passed on: Laya read
    it as commands and flagged ordinary windows (the 2026-10-05 TextEdit demo).
    """
    words = _QUOTED.findall(tree_or_text)
    if not _TREE_LINE.search(tree_or_text):
        return tree_or_text.strip()
    lines: list[str] = []
    for word in words:
        if word and (not lines or lines[-1] != word):
            lines.append(word)
    return "\n".join(lines)


def chunks(text: str, size: int = TEXT_CHUNK_CHARS) -> list[str]:
    """Text in pieces of at most `size` characters, split at line ends where possible."""
    pieces: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > size:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:size])
            line = line[size:]
        if current and len(current) + 1 + len(line) > size:
            pieces.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current.strip():
        pieces.append(current)
    return [p for p in pieces if p.strip()]
