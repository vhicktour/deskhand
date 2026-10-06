"""The Claude loop for one task.

Each turn streams one request with the whole conversation so far, runs the tool
calls in Claude's reply one at a time, in order, and appends the reply and the
results. The conversation is only ever appended to, which keeps thinking blocks
valid and the prompt cache warm. The loop is written out rather than using the
SDK's tool runner because a run must be able to stop between turns (step and
cost limits), which the runner has no public way to do. The summary is written
however the run ends, including Ctrl+C.
"""

from __future__ import annotations

import asyncio
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from anthropic.lib.tools import BetaAsyncFunctionTool, ToolError
from anthropic.types.beta import BetaMessageParam, BetaToolResultBlockParam, BetaToolUseBlock

from deskhand.bridge import CallGate, driver_tools
from deskhand.console import ConsoleView
from deskhand.local_tools import ask_user_tool, shell_tool
from deskhand.models import Limits, ModelSpec, Usage
from deskhand.prompts import system_prompt
from deskhand.questions import Ask
from deskhand.runlog import RunLog, RunSummary
from deskhand.targets.base import Session

# thinking.display "updates" returns Claude's short progress notes between tool
# calls; fallbacks "default" re-runs a request that a safety classifier declines
# on Anthropic's recommended model for that kind of refusal.
BETAS = ["thinking-display-updates-2026-08-18", "server-side-fallback-2026-07-01"]
MAX_TOKENS = 32_000


def _describe(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude rejected the credentials. Run `ant auth login` or set ANTHROPIC_API_KEY."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude API error {exc.status_code}: {exc.message} (request {exc.request_id})"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Claude API. Check the network and try again."
    return f"{type(exc).__name__}: {exc}"


def _split(content: list[Any]) -> tuple[list[str], list[str], list[Any]]:
    """A reply's progress notes, its text, and its tool_use blocks."""
    notes = [b.thinking for b in content if b.type == "thinking" and b.thinking]
    texts = [b.text for b in content if b.type == "text" and b.text]
    calls = [b for b in content if b.type == "tool_use"]
    return notes, texts, calls


async def _run_tool(
    tools: dict[str, BetaAsyncFunctionTool[Any]], call: BetaToolUseBlock
) -> BetaToolResultBlockParam:
    """One tool call as a tool_result block; failures go back to Claude marked is_error."""
    tool = tools.get(call.name)
    if tool is None:
        content: Any = f"Unknown tool: {call.name}"
    else:
        try:
            content = await tool.call(call.input)
        except ToolError as exc:
            content = exc.content
        except Exception as exc:
            content = f"{type(exc).__name__}: {exc}"
        else:
            return {"type": "tool_result", "tool_use_id": call.id, "content": content}
    return {"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": True}


async def run_task(
    *,
    task: str,
    session: Session,
    spec: ModelSpec,
    effort: str,
    limits: Limits,
    log: RunLog,
    view: ConsoleView,
    ask: Ask | None = None,
    gate: CallGate | None = None,
    client: AsyncAnthropic | None = None,
) -> RunSummary:
    hooks = [log, view, *session.hooks]
    tools: list[BetaAsyncFunctionTool[Any]] = [
        *await driver_tools(session.driver, hooks, session.driver_session, gate),
        ask_user_tool(ask or view.ask, hooks),
    ]
    if session.run_shell is not None:
        tools.append(shell_tool(session.run_shell, hooks))
    by_name = {tool.name: tool for tool in tools}
    request: dict[str, Any] = {
        "model": spec.id,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt(session.name),
        "tools": [tool.to_dict() for tool in tools],
        "thinking": {"type": "adaptive", "display": "updates"},
        "output_config": {"effort": effort},
        "cache_control": {"type": "ephemeral"},
        "fallbacks": "default",
        "betas": BETAS,
    }
    messages: list[BetaMessageParam] = [{"role": "user", "content": task}]

    client = client or AsyncAnthropic()
    usage = Usage()
    steps = 0
    status, result, error = "done", "", ""
    try:
        while True:
            async with client.beta.messages.stream(**request, messages=messages) as stream:
                message = await stream.get_final_message()
            steps += 1
            cost = usage.add(message.usage, spec)
            notes, texts, calls = _split(message.content)
            log.event(
                "turn",
                step=steps,
                model=message.model,
                stop_reason=message.stop_reason,
                notes=notes,
                text=texts,
                tool_uses=[{"id": c.id, "name": c.name, "input": c.input} for c in calls],
                usage=message.usage.to_dict(),
                cost=round(cost, 6),
            )
            view.turn(steps, notes, texts, usage.cost_usd)
            messages.append({"role": "assistant", "content": message.content})

            if message.stop_reason == "refusal":
                details = message.stop_details
                status = "refused"
                error = f"Claude declined: {details.explanation if details else 'no details'}"
                break
            if message.stop_reason == "max_tokens":
                status, error = "truncated", "The reply hit the output limit before finishing."
                break
            if message.stop_reason != "pause_turn" and not calls:
                result = "\n\n".join(texts)
                break
            if steps >= limits.max_steps:
                status, error = "step_limit", f"Stopped at the {limits.max_steps}-step limit."
                break
            if usage.cost_usd >= limits.max_cost:
                status, error = "cost_limit", f"Stopped at the ${limits.max_cost:.2f} cost limit."
                break
            if message.stop_reason == "pause_turn":
                continue  # the API carries on with a paused turn that is sent back as it is
            results = [await _run_tool(by_name, call) for call in calls]
            messages.append({"role": "user", "content": results})
    except asyncio.CancelledError:
        status, error = "interrupted", "Stopped with Ctrl+C."
        raise
    except Exception as exc:
        status, error = "error", _describe(exc)
    finally:
        summary = log.finish(
            status=status,
            result=result,
            error=error,
            steps=steps,
            cost_usd=round(usage.cost_usd, 4),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cache_read_tokens=usage.cache_read_tokens,
        )
    return summary
