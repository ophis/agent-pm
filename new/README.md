# new/

The new core ([TASK-314](https://linear.app/ophis-workgroup/issue/TASK-314)) and orchestrator ([TASK-315](https://linear.app/ophis-workgroup/issue/TASK-315)), under construction; they replace the root `core/` and `orchestrator/` at cutover. Terms: root `CONTEXT.md`; diagram: root `README.md` › Core architecture (planned). Paths below are relative to `new/`; commands run from the repo root. Each module's docstring states its behaviour.

## Layout

| Path | What |
|---|---|
| `core/src/` | the import root (`agent_pm.utils.paths.IMPORT_ROOT`): only package `agent_pm`, imported by package path; set PYTHONPATH to this tree's, never rely on an inherited one (`tests/case.py` `Case` fails a test on another tree's `agent_pm`). An agent session with commands or a gate gets it as its PYTHONPATH, replacing the user's own (`agent_pm.composer.run`) |
| `core/src/agent_pm/` | the layers, one subpackage each: `adapters/`, `composer/`, `driver/`, `delegate/` |
| `core/src/agent_pm/agent_tools/` | agent-run CLIs (git/gh side effects), outside the layers: `repo.py`, which agent sessions run as `python3 -P -m agent_pm.agent_tools.repo worktree …` / `status …` (`core/config.toml` `commands`) |
| `core/src/agent_pm/delegate/` | the Delegate: one role's run through the Composer and the Driver, read from its replies; `cli.py` its command line and the exit codes' home: `PYTHONPATH=new/core/src python3 -m agent_pm.delegate.cli --role ROLE --input TEXT --out PATH\|- --workdir DIR`, `… --list` |
| `core/src/agent_pm/utils/` | shared utilities; `config_file.py` loads and merges TOML files; `paths.py` normalizes paths and checks containment, temporary roots and printable absolute paths (`printable_abs`); `files.py` reads regular files (`read_regular`), writes one atomically (`save`) and appends a line (`append_line`); `text.py` drops control characters (`printable`); `shell.py` runs a command with a timeout, URL userinfo masked in its stderr (`sh`, `err_text`); `git.py` holds git options that keep a checkout's fsmonitor and hooks off (`GUARD`) and finds a worktree's clone (`common_dir`); `repos.py` parses repo specs and origin URLs and resolves clones and checkout paths (`resolve`, `checkout`); `markdown.py` finds Markdown code fences; `args.py` holds argparse helpers (`Once`); `protocol.py` names the reply protocol's progress mark tag and outcome fence info |
| `core/src/agent_pm/adapters/claude/` | Claude Code's Adapter, runtime `claude` |
| `core/src/agent_pm/adapters/print_prompt/` | act-as's Adapter, runtime `print_prompt`: in_process only; hands the composed prompt back as the deliverable, no process |
| `core/src/agent_pm/driver/tmux_host.py` | hosts a command in a detached tmux session and shows it in a pane or grid; the only module calling tmux (`tests/guards.py` `tmux_calls`) |
| `core/src/agent_pm/driver/modes/` | the Driver's Modes: `headless.py` (a command on pipes), `tui.py` (a tmux session through `tmux_host.py`), `in_process.py` (an Adapter's run on a thread, no process); `tui_hook.py`, the command a tui session's hooks run |
| `core/team/` | the Composer's built-in team: the root `core/team/` as on main at a0bf18c, except that `output/output.md` and `output/destinations/{local,orchestrator}.md` name the outcome's `deliverable_file`. The root `core/team/` stays the old core's, unchanged from main (`tests/unit/composer/real_test.py`) |
| `core/config.toml` | the Composer's run keys (`runtime` among them) and `[roles.<role>]` tables, the Driver's `[adapters.<name>]` tables and tui display keys (the registry reads both once per process); which layer reads which keys, and this machine's settings in `~/.agent-pm/core-new.local.toml`: the file's header |
| `orchestrator/src/` | the orchestrator's import root: only package `agent_pm_orchestrator`; on PYTHONPATH with `core/src/`, by the same rule |
| `orchestrator/src/agent_pm_orchestrator/` | the orchestrator; entries `run.py` (one issue's agent run through `delegate.run`, its events written back to Linear) and `promote.py` (a Handoff becomes the next role's Todo issue): `PYTHONPATH=new/core/src:new/orchestrator/src python3 -P -m agent_pm_orchestrator.run\|promote …`; no `router.py` or `prune.py` yet |
| `orchestrator/config.toml` | Linear ids, `[roles.<role>]`, `work_dir` and the `[core]` overlay on `core/config.toml`; `config.load` reads it once per entry's `main`; this machine's settings in `~/.agent-pm/orchestrator-new.local.toml`: the file's header |
| `core/skills/act-as/SKILL.md` | `/agent-pm:act-as` on the new core: the Delegate CLI as `env PYTHONPATH=${CLAUDE_SKILL_DIR}/../../src python3 -P -m agent_pm.delegate.cli …`, runtime `print_prompt`. `env`, not a leading `PYTHONPATH=`: Claude Code's allow rule must match the whole command, which such an assignment prevents |
| `tests/` | `case.py` (`Case`, every test's base: a fresh temp HOME per test, none set at import), `unit/`, `integration/` (the orchestrator's in their `orchestrator/`; `integration/orchestrator/` also holds `fake_linear.py` and `fake_gh.py`), `fake_adapter/` (the test-only runtime `fake`), `fake_claude.py` (a fake `claude` for the integration tests), `live_tmux.py` (private tmux servers for them), `guards.py` |
| `~/.agent-pm/tui/<name>/` | tui session `<name>`'s dir: `session.json` (its state, which `driver.api.attach` reads), `hooks.jsonl` (its hook records); `driver.api.close` removes it. `~/.agent-pm/tui/<name>.lock` serializes a name's starts, closes and stops |

Imports go one way: delegate → composer or driver, driver → adapters, a module outside the layers → driver; outside `driver/`, only `driver.api`; `agent_pm_orchestrator` → `delegate`, `composer` (`config.py` only), `driver.api`, and no `agent_pm` module imports it; any module → `utils/`, which imports only the standard library and other `utils/` modules; `agent_tools/` imports only the standard library and `utils/`, and nothing imports it. `tests/guards.py` (`layer_imports`, `pure_utils`, `orchestrator_imports`) enforces it, with the other rules in its docstring.

## Codex sandbox

`[adapters.codex].sandbox` accepts `danger-full-access` (default) or `workspace-write`. Both Modes explicitly pass `-s`, `approval_policy="on-request"` and `approvals_reviewer="auto_review"`; crew workers use the same Adapter through the Driver. Automatic review only handles actions requiring approval.

With full access, `shell_network`, writable-root boundaries and team-dir protection do not restrict the runtime. `workspace-write` retains the existing sandbox flags. Recovery saves the effective policy; resume keeps it despite config changes. Older snapshots without `sandbox` retain `workspace-write`.

## Access

A run's `read` dirs and its role's tasks dir are never `--add-dir`: no runtime is granted write to them. Codex has no read-only-dir mechanism usable here, so it adds nothing for them: under `workspace-write` its sandbox reads the whole disk and writes only its roots; under the default `danger-full-access` (Codex sandbox, above) it can write them too. Each runtime's grant and its limits: `core/config.toml`'s `read` comment.

## Tests

```bash
PYTHONPATH=new/core/src:new/orchestrator/src python3 -m unittest discover -s new/tests/unit -t . -p "*_test.py"
PYTHONPATH=new/core/src:new/orchestrator/src python3 -m unittest discover -v -s new/tests/integration -t . -p "*_test.py"
```

CI (`.github/workflows/test.yml`) runs each in its own job, and Ruff over the whole repo (root `CLAUDE.md` › Commands). Never run them in one process with the repo's `tests/`: it puts the old core's and orchestrator's flat modules (root `core/src/`, `orchestrator/src/`) on `sys.path`; nothing here imports it or the old core (`tests/guards.py` `old_core`).
