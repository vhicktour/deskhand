---
name: deskhand
description: Use real computers through deskhand - disposable Linux desktop sandboxes (up to 3 at once) and this Mac. Use when a task needs a GUI or a browser, when testing a feature end to end in a clean desktop (several scenarios in parallel), when the user asks you to do something on their Mac, or when deskhand itself fails and needs fixing.
---

# deskhand

deskhand gives you computers. Its MCP server (`deskhand`) has two kinds of tools:

- **Sandboxes**: `sandbox_create`, `sandbox_list`, `sandbox_delete`,
  `sandbox_shell`, `sandbox_screenshot`. Each sandbox is a fresh Ubuntu XFCE
  desktop with Chromium and Firefox, isolated from this Mac. At most 3 run at
  once; one idle for 30 minutes is deleted. Every sandbox shows live in the
  deskhand viewer window, where the user can watch and take over.
- **Tasks**: `task_start` hands a whole job to deskhand's own computer-use agent
  (Claude Opus 5.5 by default) in a sandbox or on this Mac, in the background;
  `task_status` checks on it (pass `wait_s` to wait up to 10 minutes);
  `task_stop` stops it.

## Which to use

- **Prefer sandboxes.** Use `on="mac"` only when the user asks for something on
  their Mac. A Mac run drives their real apps (with an orange glow on the window
  it works on), so never point it at untrusted web pages.
- **Let the agent do GUI work**: describe the task and what to check at the end
  in `task_start`. Use `sandbox_shell` for setup and for checking results, and
  `sandbox_screenshot` to see the desktop yourself.
- **Testing a local app**: the sandbox reaches this Mac at
  `host.docker.internal`, so a dev server on port 3000 is
  `http://host.docker.internal:3000` from the sandbox's browser.

## Testing a feature in parallel

Give each scenario its own sandbox, through the `sandbox-tester` agent: launch
one agent per scenario (up to 3 at once), each with the feature, the scenario
and what counts as passing. Each tester creates its sandbox, runs the scenario,
reports evidence (status, result, report path, screenshots), and deletes its
sandbox. If `sandbox_create` says the cap is reached, wait for a tester to
finish or delete a sandbox you're done with (`sandbox_list` shows owners).

## Reading results

`task_status` returns the status (done, step_limit, cost_limit, refused,
error, interrupted, crashed), the result or error, steps, cost and the
`report.html` path. The run folder next to it holds `trace.jsonl` (every model
turn and tool call), `screens/` (every screenshot), `console.log` and the logs
(`deskhand.log`; on the Mac `driver.log` and `aura.log`). Read those before
retrying a failed task. A typical sandbox task costs $0.20 to $1.

## When deskhand itself fails: fix it

You may improve deskhand when it gets in the way. It's a Python 3.12 + uv
project, normally installed editable from a git clone, so changes take effect
without reinstalling. Find the clone with
`"$(uv tool dir)/deskhand/bin/python" -c 'import deskhand; print(deskhand.__file__)'`,
which prints `<clone>/src/deskhand/__init__.py`. If the path is inside uv's tools
folder instead, deskhand isn't installed editable: tell the user what's wrong
rather than editing it.

1. Find the cause in the run folder (trace, logs, console.log).
2. Read `docs/project.md` first: it records every agreed decision. Ask the user
   before changing one.
3. Fix it in the clone, in its style (comments say what and why; one purpose
   per file; tests live in `tests/`, and the user's own rules on adding them
   apply).
4. Run the checks: `uv run ruff check . && uv run ruff format --check . &&
   uv run pyright && uv run pytest`, plus `uv run pytest -m live` when you
   touched sandboxes, the driver bridge or the agent loop (it spends about $0.20).
5. New background tasks pick up code changes at once, since each run is a new
   process. Sandbox tools come from the MCP server, which loaded the old code,
   so ask the user to reconnect it (`/mcp` → deskhand → Reconnect). Claude Code
   serves the plugin's own files (this skill, the agent, the manifests) from a
   cached copy: after changing them, bump `version` in
   `plugin/.claude-plugin/plugin.json`, then run
   `claude plugin marketplace update deskhand && claude plugin update deskhand@deskhand`.
   They apply from the next session.
6. Tell the user what you changed and why. Commit only when they ask. If you
   keep a memory, save the lasting lesson there.
