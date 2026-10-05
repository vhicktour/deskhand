# deskhand

A personal computer-use task runner. You give it a task in plain English; Claude
does the task on a real computer through Cua, and you get the result plus a
trace you can replay.

Status: approved 2026-10-05. Build from this document and ask before changing
an agreed decision.

## Goals

- Run a task end to end from one command: `deskhand run "task" --on mac`.
- Drive two kinds of computer: this Mac (its real apps) and a disposable local
  Linux sandbox.
- Let you watch live and step in: sandboxes show in deskhand's viewer window
  (v1 opened cua's browser viewer); on the Mac you see the agent's own cursor
  and keep your own.
- Show which window the agent is working on: an orange glowing border (the
  aura) around that window on the Mac, following it as the agent moves between
  windows.
- Keep every run inspectable afterwards: what Claude saw, did and said, and what
  it cost.

Users: one person (you), on this Mac (M1 Pro, 16 GB, macOS 27). No accounts,
no server, no multi-user concerns.

## Scope

v1:

| Area | In v1 |
| --- | --- |
| Targets | `--on mac` (Cua Driver on this Mac), `--on sandbox` (local Linux container on Docker Desktop, the default) |
| Interface | CLI, plus a live view of the sandbox (cua's browser viewer in v1, deskhand's viewer window since v2) |
| Model | Claude Opus 5.5 by default; `--model sonnet` (Sonnet 5.5) or `--model fable` (Fable 5.1) per run |
| Run history | A folder per run with trace, screenshots, summary and a static HTML report |
| Aura | Orange glowing border around the window the agent is working on, on the Mac |

Later (not v1): Cua Fleet cloud sandboxes (`--on cloud`, the same SDK call with
`local=False`), a local macOS VM through Lume, a web dashboard, an MCP server
so Claude Code can hand tasks to deskhand (done in v2), batch runs with cua-bench.

## How it works

```text
deskhand run "task" --on mac|sandbox
  └─ target opens a computer and an MCP session to Cua Driver on it
       mac:     `cua-driver mcp` (stdio) -> CuaDriver.app daemon (owns the macOS grants)
       sandbox: cua-sandbox creates Image.linux() locally -> its bundled Cua Driver over MCP
  └─ agent loop (Anthropic SDK)
       tools = Cua Driver's MCP tools (curated list) + deskhand's own tools
       Claude observes (window state: accessibility tree + screenshot),
       acts (click, type, keys, scroll ... in the background), verifies, repeats
  └─ run folder: trace.jsonl, screenshots, summary.json, report.html
```

Why Cua Driver tools instead of Claude's pixel computer toolset: one tool
surface for both targets, background input on the Mac (it doesn't take your
pointer or keyboard), accessibility read-back that confirms actions, and
built-in browser tools over CDP. This is cua's "Computer-Use 2.0" model.

Why our own agent loop instead of cua-agent: cua-agent 0.9.0 (and `main`) maps
unknown Claude models to the 2024 computer tool, which Claude Opus 5.5 and
Sonnet 5.5 reject; they accept computer use only as `computer_toolset_20260801`.
With Cua Driver tools we don't use either computer tool: Claude calls the
driver's tools as ordinary tools.

## Stack

Required:

| Piece | Choice | Version (2026-10-05) | Role |
| --- | --- | --- | --- |
| Language | Python 3.12, managed with uv | uv 0.12 | cua's richest high-level SDK is Python |
| Model API | `anthropic` Python SDK | latest at build time | Agent loop; auth from your `ant auth login` profile, no API key |
| Sandboxes | `cua-sandbox` (pulls the `cua` SDK) | 0.9.0 | Create, view and delete the local Linux sandbox |
| Desktop control | Cua Driver (CuaDriver.app + `cua-driver` CLI) | 0.33.4 | Driver tools on the Mac; bundled inside the sandbox image |
| MCP client | `mcp` Python SDK | latest at build time | One bridge to Cua Driver on both targets |
| CLI | Typer (with Rich) | latest at build time | Commands, live progress output |
| Aura overlay | PyObjC (`pyobjc-framework-Cocoa`, `-Quartz`) | 12.2.2 | Draws the orange border on the Mac |
| Container engine | Docker Desktop (already running) | 29.8 | Runs the sandbox (`ghcr.io/trycua/linux:24.04`, native arm64) |
| Tests | pytest + pytest-asyncio | latest | Unit tests with a fake model and a fake computer |
| Code quality | ruff (lint + format), pyright | latest | Run before every hand-off |

Optional, not in v1: the `cua` CLI (handy for `cua runtime doctor` and `cua sb
view`; the SDK doesn't need it), CI, packaging for others.

No database (run folders on disk), no hosting, no migrations, no secrets in the
repo. Cua needs no account for local sandboxes.

## CLI

```text
deskhand run "TASK" [--on sandbox|mac] [--model opus|sonnet|fable]
                    [--effort low|medium|high|xhigh|max]
                    [--max-steps 50] [--max-cost 2.00]
                    [--sandbox NAME] [--keep] [--no-view] [--no-aura]
deskhand runs                 # past runs: id, status, target, steps, cost, task
deskhand show RUN_ID          # summary in the terminal, opens report.html
deskhand sandbox create|list|rm NAME|url NAME|sweep   # named sandboxes (v2)
deskhand viewer               # the sandbox viewer window (v2)
deskhand mcp                  # the MCP server Claude Code starts (v2)
deskhand doctor               # Claude access, Cua Driver and its grants, Docker, the aura
```

- `--on sandbox` is the default; the Mac is opt-in per run.
- `--keep` leaves the sandbox running after the run; without it the sandbox is
  deleted, even on failure or Ctrl+C. `deskhand sandbox rm` deletes a kept one.
  `--sandbox NAME` works in an existing sandbox and leaves it running.
- `--no-view` skips opening the viewer window; `--no-aura` turns the border off.
- Exit codes: 0 when the task finished, 130 after Ctrl+C, 1 otherwise.

## Agent loop rules

These come from the current Claude API (Opus 5.5 / Sonnet 5.5 / Fable 5.1):

- Thinking is adaptive (it can't be turned off on Opus 5.5). Effort is set
  explicitly: default `medium`, `--effort` to change.
- `thinking.display: "updates"` (beta `thinking-display-updates-2026-08-18`) so
  the CLI can show Claude's short progress notes between tool calls.
- The history is append-only and the system prompt and `tools` array stay
  byte-identical for the whole run. Editing earlier turns invalidates thinking
  blocks and returns a 400 on new accounts. The driver's tool list is fetched
  once at the start of a run, filtered, sorted and frozen; deskhand's own tools
  are declared from the first request.
- If context ever needs trimming, use server-side context editing, never
  client-side deletion. v1 doesn't trim: the step cap keeps runs well inside the
  1M-token window.
- Prompt caching on (top-level automatic caching), so each step re-reads the
  history at the cache-read price.
- `tool_choice` stays `auto` (forced tool choice returns a 400 on these models).
- Streaming requests, final message collected with the SDK helper.
- Server-side refusal fallback on (`fallbacks: "default"`, beta
  `server-side-fallback-2026-07-01`); `stop_reason` is checked before reading
  content (`refusal`, `max_tokens`, `pause_turn`).
- A tool failure goes back to Claude as a `tool_result` with `is_error: true`.
- The loop is written out (stream a turn, run its tool calls in order, append)
  rather than using the SDK's tool runner: the runner has no public way to stop
  between turns, and breaking out of it left its generators to be closed at
  shutdown with an error. Tools still come from the SDK helpers
  (`async_mcp_tool`, `beta_async_tool`).

## Tools Claude gets

- Cua Driver's MCP tools, filtered to the ones useful for tasks: apps and
  windows, window state, screen and zoom, click/double/right click, drag,
  scroll, type, keys and shortcuts, set value, clipboard, browser page and input
  tools, verify. Deprecated, session-plumbing, cursor-styling and recording tools
  are left out, and so is `kill_app`. The list (`CURATED_TOOLS` in bridge.py)
  is 33 tools, present on both macOS and Linux, about 20k tokens of definitions,
  cached after the first turn.
- `ask_user(question)`: pauses the run and asks you in the terminal (a 2FA
  code, a choice). Declared from the first request.
- `shell(command)`: sandbox only, runs a command in the Linux sandbox (install
  or open apps, read files). Not offered on the Mac.

## The aura (Mac)

An orange glow along the edges of the window the agent is working on: a thin
line on the window's edge and a soft band that fades from the edge into the
window over about 28 points, pulsing slowly (2.6 seconds from dim to bright and
back again). Victor reviewed it on screen and approved this look on 2026-10-05.

- Which window: the bridge reads the window each driver call targets
  (`target.window_id`, or `pid` + `window_id`) and tells the aura. A desktop-wide
  action outlines the whole display. No target yet means no aura. Cua Driver's
  macOS `window_id` is the WindowServer window number Quartz uses (checked).
- Drawing: a separate helper process (`python -m deskhand.aura`, PyObjC) owns a
  borderless, transparent, click-through window covering the target window
  exactly. The fade is an inner shadow: a shape covering everything outside the
  window's rounded rectangle casts an orange shadow, masked to the rectangle. It
  is drawn inside the window's edge, so all four sides show even when the window
  touches the screen edges (an outside border lost three of them on a
  full-width window). The helper reads one-line commands on stdin
  (`window <id>`, `display`, `hide`, `show`) and exits when the run closes its
  stdin, so a crash never leaves the glow behind.
- Following the window: the helper polls the window's bounds through Quartz
  (`CGWindowListCopyWindowInfo`, no extra permission) about 15 times a second, so
  the glow tracks moves and resizes, and hides when the window closes,
  minimizes or isn't on the current Space. Apps launched while you're in a
  full-screen app open on another Space, so their glow shows when you switch
  there.
- Stacking: ordered just above the target window, so windows in front of it
  still cover the glow like a real frame (works against other apps' windows).
  The window opts out of AppKit's rule that keeps windows below the menu bar,
  which otherwise shifted it off windows near the top of the screen.
- Keeping it out of what Claude sees: macOS honors the window's "not shareable"
  setting for Cua Driver's captures. A desktop capture with the glow showing had
  no glow in it. On top of that, full-display captures hide the glow first: the
  helper answers `hide` only once the window server reports it gone, which takes
  15 to 30 ms with the window's fade animation turned off (275 ms with it).
- Not in Mission Control or Cmd-Tab, never takes focus or clicks.

Alternative considered: a small Swift helper. It would be native but adds a
compile step and a second language; PyObjC installs with uv.

## Safety

- Mac: Cua Driver in its default `standard` mode (your choice). Any app can be
  driven; the driver still denies killing processes it didn't launch, attaching
  to logged-in browser profiles without a grant, and raising OS permission
  prompts. A malicious page could steer the agent into other apps; the per-run
  app allowlist (bounded mode) is the upgrade if that becomes a concern.
- Sandbox: isolated container, deleted after the run unless `--keep`.
- Every run: step cap (default 50), cost cap (default $2, from token usage and
  the model's prices), Ctrl+C stops cleanly and still writes the run folder.
- No secrets in code, logs or run folders beyond what the screenshots show.

## Cost

- Claude: Opus 5.5 $4 / $20 per million input / output tokens, cache reads
  $0.20. Measured on the first sandbox runs: $0.20 for 6 steps and $0.25 for 11
  steps. About $0.15 of that is the first turn writing the tools and system
  prompt to the cache; each later step costs about a cent. The cap stops a run
  that goes further.
- Cua: local sandboxes and Cua Driver are free. Fleet (later) bills about
  $0.045 per vCPU-hour + $0.022 per GiB-hour.

## Run folder

`~/.local/share/deskhand/runs/<YYYYMMDD-HHMMSS>-<slug>/` (override with
`DESKHAND_HOME`):

- `trace.jsonl`: one line per event (request usage, Claude's notes, each tool
  call and its result, errors).
- `screens/NNN.png`: every image Cua Driver returned.
- `summary.json`: task, target, model, status, steps, tokens, cost, result.
- `report.html`: a static page that walks through the steps with screenshots.
- `deskhand.log` (SDK and library logs), and on the Mac `driver.log` and
  `aura.log`, so nothing but the run itself prints in the terminal.

## Project layout

```text
deskhand/
  pyproject.toml            uv project, `deskhand` entry point
  README.md                 setup and usage
  docs/project.md           this document
  src/deskhand/
    cli.py                  Typer commands
    agent.py                the Claude loop, caps and stop handling
    prompts.py              system prompt
    models.py               model aliases, IDs and prices
    bridge.py               Cua Driver's MCP tools as Claude tools, with call hooks
    local_tools.py          ask_user and the sandbox shell
    console.py              terminal output and ask_user's prompt
    doctor.py               setup checks
    targets/base.py         what an open target gives the loop
    targets/mac.py          Cua Driver on this Mac
    targets/sandbox.py      local Linux sandbox
    aura/controller.py      tells the overlay which window to outline
    aura/overlay.py         the orange border itself (helper process, PyObjC)
    runlog.py               run folder and trace
    report.py               report.html
  tests/                    pytest suite (fakes for the model and the computer)
```

## Setup you do once

- Install Cua Driver (`/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"`),
  then `cua-driver permissions grant` and accept its prompts. Toggling CuaDriver
  in System Settings alone left the daemon reporting "pending"; the grant
  command verifies the grants. Done on 2026-10-05.
- Docker Desktop running (done).
- `ant auth login` (done).
- `uv tool install --editable .` puts `deskhand` on the PATH (done).

## Verification

- Unit tests: bridge conversion, agent loop (tool calls, caps, refusal and
  error paths, Ctrl+C, append-only history), cost math, run folder writing,
  aura commands.
- Live, opt-in (`pytest -m live`): a sandbox run that opens a terminal, types a
  command, presses Enter and reads the output back from the window. Tests set a
  temporary `CUA_HOME`, because cua refuses to write the real `~/.cua` from a
  test.
- Live, manual on the Mac: cua's first-run check, compute 6 × 7 in Calculator
  and confirm it shows 42, with the aura around Calculator; move the window and
  check the border follows; check the border is absent from the saved
  screenshots.
- ruff and pyright clean.

## Risks and how the build checks them first

1. The MCP route to the sandbox's bundled Cua Driver: verified. `sb.mcp("env")`
   (cua-spacesd's `/mcp`) serves 59 driver tools.
2. Cua Driver on macOS 27: verified. Grants go through
   `cua-driver permissions grant` (it also asks for macOS's direct-capture
   consent). The Calculator run computed 6 × 7 = 42; Calculator ignored
   background clicks, so Claude followed the driver's advice and clicked once in
   the foreground.
3. Tool-list size and cost: measured (see Tools and Cost).
4. Window ids: verified, Cua Driver's macOS `window_id` equals Quartz's window
   number. In a live read-only run the glow covered exactly the window Claude
   was working on.

## v2: Claude Code plugin (approved 2026-10-05)

Goal: Claude Code uses deskhand in every session, can spin up several
sandboxes at once (one per subagent) to test a feature, and improves deskhand
itself when it hits a gap. Victor's decisions:

| Decision | Choice |
| --- | --- |
| How Claude uses computers | Both: `task_start` hands a whole job to deskhand's agent, and sandbox tools (shell, screenshot) let Claude check things itself |
| Sandbox live view | A native deskhand viewer window (PyObjC + WebKit): one tile per running sandbox, no browser tab |
| Self-improvement | Claude fixes deskhand, runs lint, types and tests, then tells Victor what changed; commits only when asked |
| Concurrent sandboxes | At most 3 (Docker has 8 GB; a sandbox with a browser uses about 1.5 GB); sandboxes idle for 30 minutes are deleted |

Pieces:

- **Sandbox manager** (`sandboxes.py`): create, list, delete, shell, screenshot
  and viewer links for named sandboxes, shared by the CLI, the MCP server and
  task runs. A small registry under `DESKHAND_HOME` records each sandbox's
  owner (the agent that made it) and when it was last used; the cap and the idle
  sweep use it.
- **Background task runs**: `task_start` starts `deskhand run` as its own
  process with a run id chosen up front, so a task survives an MCP server
  restart and picks up code changes; `task_status` reads the run folder and can
  wait for the end; `task_stop` sends Ctrl+C. `deskhand run --sandbox NAME`
  works in an existing sandbox and leaves it running.
- **MCP server** (`deskhand mcp`, stdio): `sandbox_create`, `sandbox_list`,
  `sandbox_delete`, `sandbox_shell`, `sandbox_screenshot`, `task_start`,
  `task_status`, `task_stop`.
- **Native viewer** (`deskhand viewer`): its own process, like the aura. It
  shows the sandboxes the manager lists, refreshes their viewer links before
  they expire (an hour), and replaces opening a browser tab. One runs at a time
  (it holds a lock). It opens when a sandbox is made, or when a run's own
  sandbox is up, and closes itself about ten seconds after the last sandbox is
  deleted, so a finished job leaves no window to close.
- **Plugin** (`plugin/` in this repo, installed at user scope from the repo's
  local marketplace): the MCP server, a `deskhand` skill (when to use it, the
  multi-sandbox pattern, safety, and how to improve deskhand), and a
  `sandbox-tester` subagent that tests one scenario in its own sandbox, reports
  evidence and deletes the sandbox. Code changes apply at once (deskhand is
  installed editable; `/mcp` reconnects the server). Claude Code serves the
  plugin's own files from a cached copy, so a change there needs a version
  bump and `claude plugin update`; the skill tells Claude to do both.

Verified (2026-10-05):

- Through the MCP server, as Claude Code calls it: two sandboxes created at
  once in 8 seconds, shell and screenshot in each, a `task_start` run that
  finished in 7 steps for $0.22, both deleted, an empty list afterwards.
- The viewer: three launches at once leave one window; it stays while a sandbox
  runs, closes 13 seconds after the sandbox is deleted, and closes after 10
  seconds when opened with nothing to show.
- 71 unit tests (sandbox manager, background runs, MCP tools and the viewer's
  one-window lock, on fakes), ruff and pyright clean.
