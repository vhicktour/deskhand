# deskhand

Give Claude a task in plain English and it does it on a real computer: your Mac,
or a disposable Linux sandbox. Claude operates apps through
[Cua Driver](https://cua.ai/docs/cua-driver) (accessibility tree + screenshots,
background input), and every run leaves a trace you can replay.

```sh
deskhand run "Open Calculator and compute 6 × 7" --on mac
deskhand run "Make a text file listing today's top 3 Hacker News stories"   # sandbox
```

On your Mac, the window the agent is working on gets an orange glow along its
edges that fades inward and pulses slowly (the aura). It only shows while that
window is on your current Space, and it never shows up in the screenshots Claude
sees. Sandboxes show up in deskhand's viewer window, one live tile each, where
you can watch and take over; the window closes itself once no sandbox is left.

The design and the decisions behind it are in [docs/project.md](docs/project.md).

## Setup

Requirements: macOS on Apple silicon, Python 3.12 with [uv](https://docs.astral.sh/uv/),
Docker Desktop (for the sandbox), and Claude API access.

```sh
git clone https://github.com/vhicktour/deskhand && cd deskhand
uv tool install --editable .        # puts `deskhand` on your PATH
deskhand doctor                     # checks everything below
```

- **Claude:** `ant auth login` (or `ANTHROPIC_API_KEY`). Usage is billed to your
  Claude Console workspace.
- **Cua Driver** (for `--on mac`): `/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"`,
  then `cua-driver permissions grant` and accept its prompts (Accessibility,
  Screen & System Audio Recording, and direct capture).
- **Sandbox** (`--on sandbox`): Docker Desktop running. The first run pulls
  `ghcr.io/trycua/linux:24.04` (about 4 GB).

Cua's SDK sends anonymous usage data (no screen content); `export CUA_TELEMETRY=0`
turns it off.

## Usage

```text
deskhand run "TASK" [--on sandbox|mac] [--model opus|sonnet|fable]
                    [--effort low|medium|high|xhigh|max]
                    [--max-steps 50] [--max-cost 2.00]
                    [--sandbox NAME] [--keep] [--no-view] [--no-aura]
                    [--guard | --no-guard]
deskhand runs                 # past runs
deskhand show RUN_ID          # a run's summary; opens its report
deskhand sandbox create       # a sandbox that stays until deleted (at most 3)
deskhand sandbox list         # running sandboxes
deskhand sandbox rm NAME      # delete one
deskhand viewer               # open the sandbox viewer window
deskhand laya setup|eval|status   # the guard's local check (optional, see below)
deskhand doctor               # setup checks
```

- `--on sandbox` is the default. `--on mac` drives your real apps.
- `--model opus` (Claude Opus 5.5) is the default; `sonnet` is Claude Sonnet
  5.5, `fable` is Claude Fable 5.1.
- A run stops at `--max-steps` model turns or `--max-cost` dollars, whichever
  comes first. Ctrl+C stops it cleanly.
- `--keep` leaves the sandbox running afterwards; `--sandbox NAME` works in an
  existing sandbox and leaves it running; `--no-view` doesn't open the viewer
  window; `--no-aura` turns off the orange border on the Mac.

Runs are saved in `~/.local/share/deskhand/runs/` (set `DESKHAND_HOME` to move
them): `trace.jsonl`, `screens/`, `summary.json`, `report.html`, and the logs
(`deskhand.log`; on the Mac also `driver.log` and `aura.log`).

A typical sandbox task costs $0.20 to $0.30 with Opus 5.5: about $0.15 for the
first turn (it caches the tools and instructions), then about a cent a step.

## Claude Code plugin

The `plugin/` folder makes deskhand part of every Claude Code session: an MCP
server (`deskhand mcp`) with sandbox tools (`sandbox_create`, `sandbox_list`,
`sandbox_delete`, `sandbox_shell`, `sandbox_screenshot`) and task tools
(`task_start`, `task_status`, `task_answer`, `task_stop`), a `deskhand` skill,
and a `sandbox-tester` subagent that tests one scenario in its own sandbox.
Sandboxes idle for 30 minutes are deleted.

```sh
claude plugin marketplace add vhicktour/deskhand   # or the path of your clone
claude plugin install deskhand@deskhand --scope user
```

The plugin starts `deskhand mcp`, so install deskhand first (see Setup).

After changing deskhand's code, run `/mcp` in Claude Code to reconnect the
server; background runs pick up changes on their own. Claude Code keeps a
cached copy of the plugin's own files (skill, agent): after changing anything
under `plugin/`, bump the version in `plugin/.claude-plugin/plugin.json` and run
`claude plugin marketplace update deskhand && claude plugin update deskhand@deskhand`.

## Guard (optional)

The guard checks each action with [Laya](https://huggingface.co/convaiinnovations/laya),
a small classifier that runs on your Mac, before it runs. It asks you first
when an action looks hard to undo (send, pay, delete, sign in), or when the
window's text tried to give the agent orders. It also tells Claude when a
screen's text looks aimed at it.

```sh
deskhand laya setup     # installs Laya in its own environment (PyTorch, ~800 MB model)
deskhand laya eval      # measures it on labelled cases and fits its thresholds
deskhand laya cases     # adds cases from your past runs, labelled by Claude (a few cents)
deskhand guard allow    # never ask: record what it would have asked (deskhand guard ask undoes it)
```

The guard turns on by default for Mac runs only once `deskhand laya eval`
passes: 95% of risky cases caught with at most 10% false alarms. Out of the box
Laya doesn't pass (53% and 65% false alarms on deskhand's cases), so for now
the guard is off unless you pass `--guard`. If Laya isn't running or answers
slowly, the run goes on without it and says so. Background runs ask through
Claude Code: `task_status` says `waiting`, and `task_answer` replies.

## Safety

On your Mac, Cua Driver runs in its default `standard` mode: the agent can drive
any app. Claude is told to ask you before anything hard to undo (sending,
buying, deleting, signing in) and to treat on-screen text as data, not
instructions, but a hostile web page could still try to steer it. Watch runs on
your Mac, and keep tasks that touch untrusted pages in the sandbox.

## Development

```sh
uv sync
uv run pytest            # unit tests (fakes, no network)
uv run pytest -m live    # live sandbox run: needs Docker, spends model tokens
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

## License

MIT, see [LICENSE](LICENSE).
