"""report.html: a static page that replays a run step by step.

Built from the run's trace and summary when the run ends, with no scripts and
no network, so it opens from disk anywhere. Each model turn shows Claude's
progress notes and text; each tool call shows its arguments, its text result
(long results fold away) and any screenshots, linked from screens/. Everything
from the trace is HTML-escaped: page text the agent read could contain markup.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deskhand.runlog import RunSummary

_PREVIEW_CHARS = 1500

_CSS = """
:root { --bg: #fbfaf8; --fg: #1d1b19; --muted: #6b6560; --card: #ffffff; --line: #e7e2dc;
        --accent: #ff7a1a; --error: #c62828; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #171513; --fg: #ece8e3; --muted: #a39b93; --card: #211e1b; --line: #36312c;
          --error: #ef5350; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 22px; margin: 0 0 6px; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
.box { border-left: 4px solid var(--accent); background: var(--card); padding: 12px 16px;
       border-radius: 6px; margin-bottom: 24px; white-space: pre-wrap; }
.box.error { border-color: var(--error); }
.step { margin: 28px 0 8px; font-size: 13px; font-weight: 600; color: var(--accent);
        text-transform: uppercase; letter-spacing: .04em; }
.note { color: var(--muted); font-style: italic; margin: 4px 0; white-space: pre-wrap; }
.say { margin: 6px 0; white-space: pre-wrap; }
.tool { background: var(--card); border: 1px solid var(--line); border-radius: 8px;
        padding: 10px 14px; margin: 10px 0; }
.tool.error { border-color: var(--error); }
.tool .name { font-family: ui-monospace, Menlo, monospace; font-weight: 600; }
.tool .args { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--muted);
              word-break: break-all; }
pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; margin: 8px 0 0; }
img { max-width: 100%; border: 1px solid var(--line); border-radius: 6px; margin-top: 8px; }
details summary { cursor: pointer; color: var(--muted); font-size: 12px; }
"""


def _e(text: Any) -> str:
    return html.escape(str(text))


def _args_line(arguments: dict[str, Any]) -> str:
    return _e(json.dumps(arguments, ensure_ascii=False, sort_keys=True))


def _text_block(text: str) -> str:
    if not text:
        return ""
    if len(text) <= _PREVIEW_CHARS:
        return f"<pre>{_e(text)}</pre>"
    return (
        f"<pre>{_e(text[:_PREVIEW_CHARS])}…</pre>"
        f"<details><summary>Full result ({len(text):,} characters)</summary>"
        f"<pre>{_e(text)}</pre></details>"
    )


def _turn(event: dict[str, Any]) -> str:
    parts = [f'<div class="step">Step {_e(event.get("step", "?"))}</div>']
    parts += [f'<div class="note">{_e(note)}</div>' for note in event.get("notes", [])]
    parts += [f'<div class="say">{_e(text)}</div>' for text in event.get("text", [])]
    return "\n".join(parts)


def _tool(event: dict[str, Any]) -> str:
    classes = "tool error" if event.get("is_error") else "tool"
    images = "".join(
        f'<a href="{_e(path)}"><img src="{_e(path)}" alt="Screenshot from {_e(event["name"])}"></a>'
        for path in event.get("images", [])
    )
    return (
        f'<div class="{classes}"><div class="name">{_e(event["name"])}</div>'
        f'<div class="args">{_args_line(event.get("arguments", {}))}</div>'
        f"{_text_block(event.get('text', ''))}{images}</div>"
    )


def _ask(event: dict[str, Any]) -> str:
    return (
        f'<div class="tool"><div class="name">ask_user</div>'
        f"<pre>Q: {_e(event.get('question', ''))}\nA: {_e(event.get('answer', ''))}</pre></div>"
    )


def render(summary: RunSummary, events: list[dict[str, Any]]) -> str:
    meta = " · ".join(
        [
            _e(summary.target),
            _e(summary.model),
            f"effort {_e(summary.effort)}",
            f"<b>{_e(summary.status)}</b>",
            f"{summary.steps} steps",
            f"{summary.tool_calls} tool calls",
            f"${summary.cost_usd:.2f}",
            f"{_e(summary.started_at)} → {_e(summary.ended_at)}",
        ]
    )
    body = [f"<h1>{_e(summary.task)}</h1>", f'<div class="meta">{meta}</div>']
    if summary.result:
        body.append(f'<div class="box">{_e(summary.result)}</div>')
    if summary.error:
        body.append(f'<div class="box error">{_e(summary.error)}</div>')
    renderers = {"turn": _turn, "tool": _tool, "ask": _ask}
    body += [renderers[e["kind"]](e) for e in events if e["kind"] in renderers]
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>deskhand run</title><style>{_CSS}</style></head>"
        f"<body><main>{''.join(body)}</main></body></html>"
    )


def write(run_dir: Path, summary: RunSummary, events: list[dict[str, Any]]) -> Path:
    path = run_dir / "report.html"
    path.write_text(render(summary, events), encoding="utf-8")
    return path
