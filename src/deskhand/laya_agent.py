"""A run with no Claude in it: Cua reads the window, laya-browser picks each action.

Each step reads the target window's accessibility tree through Cua (no
screenshot, the fast path) and turns its elements into the page laya-browser
was trained on: buttons and links to click, fields to fill, plus scroll and
Enter. The BRAIN server picks one operation in about 0.2 s, and Cua does it.
Laya can't write, so anything typed comes from the task itself: its quoted
values and URLs, in order. A run ends when Laya says DONE or BLOCKED, at the
step limit, when an action keeps changing nothing, or when Laya is only
guessing ("unsure").
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2
from mcp.types import CallToolResult, TextContent

from deskhand.bridge import CallGate, CallHook, DriverBridge, driver_bridge
from deskhand.console import ConsoleView
from deskhand.laya import server
from deskhand.laya.server import BRAIN, LayaUnavailable
from deskhand.runlog import RunLog, RunSummary
from deskhand.snapshot import Snapshot, visible_text
from deskhand.targets.base import Session

# laya-browser judges more than 60 options in two passes, so per step it gets at
# most these many on-screen elements, in page order; Scroll down shows more.
MAX_CLICKS = 55
MAX_FILLS = 20
STUCK_REPEATS = 3
UNSURE_BELOW = 0.1  # Laya's own confidence in its pick; good steps ran 0.6 to 0.85
UNSURE_STEPS = 1  # a near-guess isn't carried out: it once clicked "More filters"
DECIDE_TIMEOUT_S = 30.0
START_WAIT_S = 180.0  # a first start loads (or downloads) laya-browser

# Cua's accessibility roles (macOS AX and Linux AT-SPI) as the web roles the
# model was trained on. Password fields are left out: nothing types passwords.
_ROLES = {
    "button": "button",
    "pushbutton": "button",
    "togglebutton": "button",
    "menubutton": "button",
    "popupbutton": "button",
    "link": "link",
    "textfield": "textbox",
    "textarea": "textbox",
    "entry": "textbox",
    "text": "textbox",
    "searchfield": "searchbox",
    "combobox": "combobox",
    "checkbox": "checkbox",
    "radiobutton": "radio",
    "switch": "switch",
    "menuitem": "menuitem",
    "tab": "tab",
    "pagetab": "tab",
    "cell": "option",
    "row": "option",
    "listitem": "option",
    "option": "option",
}
_FILLABLE = {"textbox", "searchbox", "combobox"}
# The page itself in a browser window (macOS AXWebArea, Linux "document web").
_WEB_AREAS = {"webarea", "documentweb", "documentframe"}
_QUOTED = re.compile(r"\"([^\"]{1,300})\"|“([^”]{1,300})”|(?<![\w])'([^']{1,300})'(?![\w])")
_URL = re.compile(r"https?://[^\s\"'”’)]+")
_SKIP_WINDOWS = {"xfdesktop", "xfce4-panel", "dock", "window server", "control center"}


def web_role(role: str) -> str | None:
    return _ROLES.get(role.removeprefix("AX").replace(" ", "").lower())


def typed_values(task: str) -> list[str]:
    """The text a run may type, in the order the task gives it: quoted values, then URLs."""
    values = [next(g for g in m.groups() if g) for m in _QUOTED.finditer(task)]
    values += [u.rstrip(".,;:!?") for u in _URL.findall(task)]
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


@dataclass
class Window:
    pid: int
    window_id: int
    app: str
    title: str


def pick_window(windows: list[dict[str, Any]], task: str) -> Window | None:
    """The window the task most likely means: its app named in the task, then frontmost."""
    wanted = task.lower()
    best: tuple[tuple[int, int], Window] | None = None
    for w in windows:
        title, app = str(w.get("title") or ""), str(w.get("app_name") or "")
        if not title or app.lower() in _SKIP_WINDOWS or "pid" not in w or "window_id" not in w:
            continue
        # "Chrome" in a task names "Google Chrome": any longer word of the app's name counts.
        named = any(word in wanted for word in app.lower().split() if len(word) > 3)
        score = 3 * named + 2 * bool(w.get("on_current_space", True))
        score += sum(word in wanted for word in title.lower().split() if len(word) > 3)
        rank = (score, -int(w.get("z_index", 0)))
        if best is None or rank > best[0]:
            best = (rank, Window(int(w["pid"]), int(w["window_id"]), app, title))
    return best[1] if best else None


def _web_page(snapshot: Snapshot) -> tuple[int, set[int]] | None:
    """In a browser window, the visible tab's web page and its elements' indexes.

    laya-browser was trained on page content only. Offered a browser's own
    toolbar too, it typed a search into Firefox's address bar instead of the
    page's search box, and with the toolbar's words first in the page text it
    never saw the results it was looking for. Each tab has a web page in the
    tree, so the visible one is the one the window title names.
    """
    elements = snapshot.elements
    pages = [
        e
        for e in elements
        if str(e.get("role", "")).removeprefix("AX").replace(" ", "").lower() in _WEB_AREAS
    ]
    if not pages:
        return None
    named = [p for p in pages if (p.get("label") or "") and str(p["label"]) in snapshot.window]
    root = int((named or pages)[0].get("element_index", -1))
    by_index = {e.get("element_index"): e for e in elements}
    inside: set[int] = set()
    for element in elements:
        node: dict[str, Any] | None = element
        for _ in range(200):  # a tree is never this deep; stops a parent cycle
            if node is None:
                break
            if node.get("element_index") == root:
                inside.add(int(element.get("element_index", -1)))
                break
            node = by_index.get(node.get("parent_index"))
    return root, inside


def _field(label: str, value: str, typed: set[str]) -> tuple[str, str]:
    """A text field's name and contents as Laya should see them.

    When a field has no name, Cua names it after its contents, and an empty
    field reports its placeholder as both name and contents. On LinkedIn that
    showed Laya a field called "software engineer" already holding "software
    engineer", which it kept retyping. Text the run typed means the former
    (a generic name); otherwise it's a placeholder (an empty field).
    """
    if not value or value != label:
        return label, value
    return ("text field", value) if value in typed else (label, "")


def _rect(frame: Any) -> tuple[float, float, float, float] | None:
    """An element or window frame as (x, y, width, height), from either key style."""
    if not isinstance(frame, dict):
        return None
    parts = [frame.get(k) for k in ("x", "y")]
    parts += [frame.get("w", frame.get("width")), frame.get("h", frame.get("height"))]
    if any(not isinstance(v, int | float) for v in parts):
        return None
    x, y, w, h = (float(v) for v in parts)  # type: ignore[arg-type]
    return x, y, w, h


def _on_screen(element: dict[str, Any], view: tuple[float, float, float, float] | None) -> bool:
    """Whether an element shows in the visible area; elements without a frame count."""
    box = _rect(element.get("frame"))
    if view is None or box is None:
        return True
    x, y, w, h = box
    vx, vy, vw, vh = view
    return x < vx + vw and x + w > vx and y < vy + vh and y + h > vy


def to_page(
    snapshot: Snapshot, typed: set[str] | None = None
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """A window as laya-browser's page, and each action id's element.

    Only elements on screen are offered (in a browser, inside the page's visible
    area), at most MAX_CLICKS to click and MAX_FILLS to fill: LinkedIn's page
    offered 150, which laya-browser judges in two passes. Scrolling shows more.
    """
    actions: list[dict[str, Any]] = []
    elements: dict[str, dict[str, Any]] = {}
    url = ""
    web = _web_page(snapshot)
    page_only = web[1] if web else None
    root = next((e for e in snapshot.elements if web and e.get("element_index") == web[0]), None)
    view = _rect(root.get("frame")) if root else _rect(snapshot.bounds)
    counts = {"click": 0, "fill": 0}
    for element in snapshot.elements:
        role = web_role(str(element.get("role", "")))
        words = snapshot.words(element)
        if role is None or not words or element.get("enabled") is False:
            continue
        value = str(element.get("value") or "")
        if role in _FILLABLE and any(
            k in words.lower() for k in ("address", "url", "enter address")
        ):
            url = url or value
        if page_only is not None and int(element.get("element_index", -1)) not in page_only:
            continue  # the browser's own toolbar, tabs and menus
        kind = "fill" if role in _FILLABLE else "click"
        if not _on_screen(element, view) or counts[kind] >= (
            MAX_FILLS if kind == "fill" else MAX_CLICKS
        ):
            continue
        counts[kind] += 1
        node = str(element.get("element_token"))
        action: dict[str, Any] = {"node": node, "label": words, "role": role}
        if role in ("checkbox", "radio", "switch"):
            action["checked"] = value in ("1", "true", "True")
        if role in _FILLABLE:
            name, contents = _field(words, value, typed or set())
            action.update(
                id=f"e{len(actions) + 1}", kind="fill", label=name, current_value=contents
            )
        else:
            action.update(id=f"e{len(actions) + 1}", kind="click")
        actions.append(action)
        elements[action["id"]] = element
    actions += [
        {"id": "scroll_down", "kind": "scroll", "label": "Scroll down"},
        {"id": "press_enter", "kind": "key", "label": "Press Enter"},
    ]
    text = snapshot.text_under(web[0]) if web else visible_text(snapshot.tree)
    return {"url": url, "title": snapshot.window, "text": text, "actions": actions}, elements


class Decider(Protocol):
    """What the loop needs from laya-browser (Brain is the real one)."""

    async def decide(
        self, page: dict[str, Any], goal: str, history: list[dict[str, Any]]
    ) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class Brain:
    """laya-browser on the local BRAIN server; started on first use."""

    def __init__(self, http: httpx2.AsyncClient | None = None, start: bool = True) -> None:
        self._http = http or httpx2.AsyncClient()
        self._ready = not start

    async def decide(
        self, page: dict[str, Any], goal: str, history: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not self._ready:
            await asyncio.to_thread(server.ensure, START_WAIT_S, BRAIN)
            self._ready = True
        server.touch(BRAIN)
        reply = await self._http.post(
            f"{server.url(BRAIN)}/decide",
            headers=server.headers(),
            json={"page": page, "goal": goal, "history": history},
            timeout=DECIDE_TIMEOUT_S,
        )
        reply.raise_for_status()
        return reply.json()

    async def aclose(self) -> None:
        await self._http.aclose()


def _text(result: CallToolResult) -> str:
    return " ".join(b.text for b in result.content if isinstance(b, TextContent))


def _fingerprint(page: dict[str, Any]) -> str:
    return hashlib.sha1(f"{page['title']}\n{page['text'][:4000]}".encode()).hexdigest()


async def _act(bridge: DriverBridge, name: str, args: dict[str, Any]) -> CallToolResult:
    """One driver call; retried in the foreground when background input was refused."""
    result = await bridge.call_tool(name, args)
    if result.is_error and "foreground" in _text(result).lower():
        result = await bridge.call_tool(name, {**args, "delivery_mode": "foreground"})
    return result


async def _type(
    bridge: DriverBridge, where: dict[str, Any], token: str, text: str
) -> CallToolResult:
    """Type into a field; when background typing didn't land, click it and type for real.

    Cua's background typing into Firefox's page fields (through Linux
    accessibility) reported "reads back '', which does not contain the typed
    text": nothing landed, and Laya then said DONE on an empty search. A
    foreground click focuses the field, and real keystrokes reach it. Keys go
    out with no delay between them: Cua's default 30 ms made a 22-character
    search take 5.1 s in Chrome, against 3.1 s without (measured 2026-10-05).
    """
    args = {**where, "element_token": token, "text": text, "delay_ms": 0}
    result = await _act(bridge, "type_text", args)
    if result.is_error or "does not contain the typed text" in _text(result):
        focus = {**where, "element_token": token, "delivery_mode": "foreground"}
        await bridge.call_tool("click", focus)
        typing = {**where, "text": text, "delay_ms": 0, "delivery_mode": "foreground"}
        result = await bridge.call_tool("type_text", typing)
    return result


async def run_laya_task(
    *,
    task: str,
    session: Session,
    log: RunLog,
    view: ConsoleView,
    max_steps: int,
    gate: CallGate | None = None,
    brain: Decider | None = None,
) -> RunSummary:
    hooks: Sequence[CallHook] = [log, view, *session.hooks]
    bridge = await driver_bridge(session.driver, hooks, session.driver_session, gate)
    brain = brain or Brain()
    values = typed_values(task)
    history: list[dict[str, Any]] = []
    status, result, error, steps = "error", "", "", 0
    last_print, repeats, unsure = "", 0, 0
    try:
        listing = await bridge.call_tool("list_windows", {})
        windows = (listing.structured_content or {}).get("windows") or []
        window = pick_window(windows, task)
        if window is None:
            status, error = "blocked", "No window to work in; open the app first."
            return log.finish(status=status, error=error)
        view.info(f"Working in {window.app} — {window.title}")
        where = {"pid": window.pid, "window_id": window.window_id}
        while True:
            state = await bridge.call_tool(
                "get_window_state", {**where, "include_screenshot": False, "max_elements": 600}
            )
            snapshot = Snapshot.from_structured(state.structured_content)
            if state.is_error or snapshot is None:
                status, error = "error", f"Couldn't read the window: {_text(state)[:300]}"
                break
            page, elements = to_page(snapshot, {h["text"] for h in history if h.get("text")})
            fingerprint = _fingerprint(page)
            if history:
                history[-1]["page_changed"] = fingerprint != last_print
            last_print = fingerprint
            decision = await brain.decide(page, task, history)
            steps += 1
            operation, action = decision["operation"], decision.get("action") or {}
            label = str(action.get("label", ""))
            note = (
                f"{operation} {label}".strip()
                + f" ({decision['confidence']:.2f}, {decision['ms']:.0f} ms)"
            )
            log.event("turn", step=steps, model="laya-browser", notes=[note], text=[], cost=0.0,
                      decision=decision, page_title=page["title"])  # fmt: skip
            view.turn(steps, [note], [], 0.0)
            if operation in ("DONE", "BLOCKED"):
                status = "done" if operation == "DONE" else "blocked"
                result = f'Laya said {operation} after {steps} steps on "{page["title"]}".'
                break
            same = history and history[-1]["action"] == label and not history[-1]["page_changed"]
            repeats = repeats + 1 if same else 0
            if repeats >= STUCK_REPEATS - 1:
                status, error = "stuck", f'Laya kept choosing "{label}" and nothing changed.'
                break
            # After finishing a search Laya often never says DONE: it fumbles at
            # 3 to 6% confidence (clicking "More filters", "All filters") until the
            # stuck check, 20 s later. A near-guess ends the run before it's carried
            # out, reported as unsure, not done.
            unsure = unsure + 1 if decision["confidence"] < UNSURE_BELOW else 0
            if unsure >= UNSURE_STEPS:
                status = "unsure"
                error = f'Laya wasn\'t sure what to do next on "{page["title"]}"; check the page.'
                break
            typed = None
            element = elements.get(str(action.get("id", "")))
            if operation == "TYPE_TEXT" and element is not None:
                # A field Laya comes back to gets its text again; a new field gets
                # the task's next value not typed anywhere yet. A task with a single
                # value reuses it: a search box often reappears as a new field on
                # the results page (LinkedIn's did).
                earlier = [h["text"] for h in history if h.get("text") and h["action"] == label]
                used = {h["text"] for h in history if h.get("text")}
                typed = earlier[-1] if earlier else next((v for v in values if v not in used), None)
                if typed is None and len(values) == 1:
                    typed = values[0]
                if typed is None:
                    status = "blocked"
                    error = (
                        f'Laya chose to type into "{label}", but the task gives no text to type.'
                    )
                    break
                token = element["element_token"]
                current = str(action.get("current_value") or "")
                if current.strip() == typed:
                    # Already there: retyping only appended a second copy (Chrome
                    # ignores clearing a page field by setting its value), so the
                    # text is submitted instead.
                    view.info(f'"{label}" already holds the text; pressing Enter instead.')
                    outcome = await _act(bridge, "press_key", {**where, "key": "enter"})
                else:
                    if current:  # other text: select it all, so typing replaces it
                        keys = ["cmd", "a"] if session.name == "mac" else ["ctrl", "a"]
                        await _act(
                            bridge, "hotkey", {**where, "element_token": token, "keys": keys}
                        )
                    outcome = await _type(bridge, where, token, typed)
            elif operation in ("CLICK", "SELECT") and element is not None:
                outcome = await _act(
                    bridge, "click", {**where, "element_token": element["element_token"]}
                )
            elif operation == "SCROLL_DOWN":
                outcome = await _act(bridge, "scroll", {**where, "direction": "down", "amount": 5})
            elif operation == "PRESS_ENTER":
                outcome = await _act(bridge, "press_key", {**where, "key": "enter"})
            else:
                status, error = (
                    "blocked",
                    f"Laya chose {operation}, which this loop can't carry out.",
                )
                break
            history.append({"action": label or operation, "kind": operation.lower(), "text": typed,
                            "page_changed": True, "ok": not outcome.is_error})  # fmt: skip
            if steps >= max_steps:
                status, error = "step_limit", f"Stopped at the {max_steps}-step limit."
                break
    except asyncio.CancelledError:
        status, error = "interrupted", "Stopped with Ctrl+C."
        raise
    except (LayaUnavailable, httpx2.HTTPError) as exc:
        status, error = "error", f"laya-browser didn't answer: {exc}"
    except Exception as exc:  # a broken Cua connection, say: report it, don't crash the run
        status, error = "error", f"{type(exc).__name__}: {exc}"
    finally:
        await brain.aclose()
        summary = log.finish(status=status, result=result, error=error, steps=steps, cost_usd=0.0)
    return summary
