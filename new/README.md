# new/

The new core ([TASK-314](https://linear.app/ophis-workgroup/issue/TASK-314)) and orchestrator ([TASK-315](https://linear.app/ophis-workgroup/issue/TASK-315)), under construction; they replace the root `core/` and `orchestrator/` at cutover. Terms: root `CONTEXT.md`; diagram: root `README.md` › Core architecture (planned). Paths below are relative to `new/`; commands run from the repo root. Each module's docstring states its behaviour.

## Layout

| Path | What |
|---|---|
| `core/src/` | the layers, one package each: `adapters/`, `composer/`, `driver/`, `delegate/`. `repo.py`, a module outside the layers |
| `core/src/delegate/` | the Delegate: one role's run through the Composer and the Driver, read from its replies; `cli.py` its command line and the exit codes' home: `python3 new/core/src/delegate/cli.py --role ROLE --input TEXT --out PATH\|- --workdir DIR`, `… --list` |
| `core/src/utils/` | shared utilities; `config_file.py` loads and merges TOML files; `paths.py` normalizes paths and checks containment, temporary roots and printable absolute paths (`printable_abs`); `files.py` reads regular files (`read_regular`), writes one atomically (`save`) and appends a line (`append_line`); `text.py` drops control characters (`printable`); `shell.py` runs a command with a timeout, URL userinfo masked in its stderr (`sh`, `err_text`); `git.py` holds git options that keep a checkout's fsmonitor and hooks off (`GUARD`) and finds a worktree's clone (`common_dir`); `repos.py` parses repo specs and origin URLs and resolves clones and checkout paths (`resolve`, `checkout`); `markdown.py` finds Markdown code fences; `args.py` holds argparse helpers (`Once`); `protocol.py` names the reply protocol's progress mark tag and outcome fence info |
| `core/src/adapters/claude/` | Claude Code's Adapter, runtime `claude` |
| `core/src/adapters/print_prompt/` | act-as's Adapter, runtime `print_prompt`: in_process only; hands the composed prompt back as the deliverable, no process |
| `core/src/driver/tmux_host.py` | hosts a command in a detached tmux session and shows it in a pane or grid; the only module calling tmux (`tests/guards.py` `tmux_calls`) |
| `core/src/driver/modes/` | the Driver's Modes: `headless.py` (a command on pipes), `tui.py` (a tmux session through `tmux_host.py`), `in_process.py` (an Adapter's run on a thread, no process); `tui_hook.py`, the command a tui session's hooks run |
| `core/team/` | the Composer's built-in team: the root `core/team/` as on main at a0bf18c, except that `output/output.md` and `output/destinations/{local,orchestrator}.md` name the outcome's `deliverable_file`. The root `core/team/` stays the old core's, unchanged from main (`tests/unit/composer/real_test.py`) |
| `core/config.toml` | the Composer's run keys (`runtime` among them) and `[roles.<role>]` tables, the Driver's `[adapters.<name>]` tables and tui display keys (the registry reads both once per process); which layer reads which keys, and this machine's settings in `~/.agent-pm/core-new.local.toml`: the file's header |
| `orchestrator/src/` | the orchestrator; entries `run.py` (one issue's agent run through `delegate.run`, its events written back to Linear) and `promote.py` (a Handoff becomes the next role's Todo issue); no `router.py` or `prune.py` yet |
| `orchestrator/config.toml` | Linear ids, `[roles.<role>]`, `work_dir` and the `[core]` overlay on `core/config.toml`; `config.load` reads it once per entry's `main`; this machine's settings in `~/.agent-pm/orchestrator-new.local.toml`: the file's header |
| `core/skills/act-as/SKILL.md` | `/agent-pm:act-as` on the new core (Delegate CLI, runtime `print_prompt`) |
| `tests/` | `unit/`, `integration/` (the orchestrator's in their `orchestrator/`; `integration/orchestrator/` also holds `fake_linear.py` and `fake_gh.py`), `hermetic.py` (a temp HOME, so no test reads a real local file), `fake_adapter/` (the test-only runtime `fake`), `fake_claude.py` (a fake `claude` for the integration tests), `live_tmux.py` (private tmux servers for them), `guards.py` |
| `~/.agent-pm/tui/<name>/` | tui session `<name>`'s dir: `session.json` (its state, which `driver.api.attach` reads), `hooks.jsonl` (its hook records); `driver.api.close` removes it. `~/.agent-pm/tui/<name>.lock` serializes a name's starts, closes and stops |

Imports go one way: delegate → composer or driver, driver → adapters, a module outside the layers → driver; outside `driver/`, only `driver.api`; `orchestrator/src/` → `delegate`, `composer` (`config.py` only), `driver.api`; any module → `utils/`, which imports only the standard library and other `utils/` modules. `tests/guards.py` (`layer_imports`, `pure_utils`, `orchestrator_imports`) enforces it, with the other rules in its docstring.

## Tests

```bash
python3 -m unittest $(find new/tests/unit -name '*_test.py' -not -path '*/orchestrator/*' | sed 's#/#.#g; s#\.py$##')
python3 -m unittest -v $(find new/tests/integration -name '*_test.py' -not -path '*/orchestrator/*' | sed 's#/#.#g; s#\.py$##')
python3 -m unittest discover -s new/tests/unit/orchestrator -t . -p "*_test.py"
python3 -m unittest discover -v -s new/tests/integration/orchestrator -t . -p "*_test.py"
```

CI (`.github/workflows/test.yml`) runs each in its own job, and Ruff over the whole repo (root `CLAUDE.md` › Commands). Never run them in one process with the repo's `tests/`: it puts the root `core/src/` and `orchestrator/src/` on `sys.path`, whose module names (`repo`, `config`, `linear`, …) are the same as here; nothing here imports it or the old core (root `core/src/`) (`tests/guards.py` `old_core`).
