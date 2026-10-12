# new/

The new core, under construction ([TASK-314](https://linear.app/ophis-workgroup/issue/TASK-314)); it replaces the root `core/` at cutover. Terms: root `CONTEXT.md`; diagram: root `README.md` › Core architecture (planned). Paths below are relative to `new/`; commands run from the repo root. Each module's docstring states its behaviour.

## Layout

| Path | What |
|---|---|
| `core/src/` | the layers, one package each: `adapters/`, `composer/`, `driver/`, `delegate/`. `repo.py`, a module outside the layers |
| `core/src/delegate/` | the Delegate: one role's run through the Composer and the Driver, read from its replies; `cli.py` its command line and the exit codes' home: `python3 new/core/src/delegate/cli.py --role ROLE --input TEXT --out PATH --workdir DIR`, `… --list` |
| `core/src/utils/` | shared utilities; `config_file.py` loads and merges TOML files; `paths.py` normalizes paths and checks containment, temporary roots and printable absolute paths (`printable_abs`); `files.py` reads regular files (`read_regular`); `markdown.py` finds Markdown code fences; `args.py` holds argparse helpers (`Once`) |
| `core/src/adapters/claude/` | Claude Code's Adapter, runtime `claude` |
| `core/src/driver/tmux_host.py` | hosts a command in a detached tmux session and shows it in a pane or grid; the only module calling tmux (`tests/guards.py` `tmux_calls`) |
| `core/src/driver/modes/` | the Driver's Modes: `headless.py` (a command on pipes), `tui.py` (a tmux session through `tmux_host.py`), `in_process.py` (an Adapter's run on a thread, no process); `tui_hook.py`, the command a tui session's hooks run |
| `core/team/` | the Composer's built-in team: the root `core/team/` as on main at a0bf18c, except that `output/output.md` and `output/destinations/{local,orchestrator}.md` name the outcome's `deliverable_file`. The root `core/team/` stays the old core's, unchanged from main (`tests/unit/composer/real_test.py`) |
| `core/config.toml` | the Composer's run keys (`runtime` among them) and `[roles.<role>]` tables, the Driver's `[adapters.<name>]` tables and tui display keys (the registry reads both once per process); which layer reads which keys, and this machine's settings in `~/.agent-pm/core-new.local.toml`: the file's header |
| `tests/` | `unit/`, `integration/`, `fake_adapter/` (the test-only runtime `fake`), `fake_claude.py` (a fake `claude` for the integration tests), `live_tmux.py` (private tmux servers for them), `guards.py` |
| `~/.agent-pm/tui/<name>/` | tui session `<name>`'s dir: `session.json` (its state, which `driver.api.attach` reads), `hooks.jsonl` (its hook records); `driver.api.close` removes it. `~/.agent-pm/tui/<name>.lock` serializes a name's starts, closes and stops |

Imports go one way: delegate → composer or driver, driver → adapters, a module outside the layers → driver; outside `driver/`, only `driver.api`; any module → `utils/`, which imports only the standard library. `tests/guards.py` (`layer_imports`, `pure_utils`) enforces it, with the other rules in its docstring.

## Tests

```bash
python3 -m unittest discover -s new/tests/unit -t . -p "*_test.py"
python3 -m unittest discover -v -s new/tests/integration -t . -p "*_test.py"
```

CI (`.github/workflows/test.yml`) runs each in its own job. Never run them in one process with the repo's `tests/`: nothing here imports it or the old core (root `core/src/`) (`tests/guards.py` `old_core`).
