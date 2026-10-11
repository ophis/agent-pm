# new/

The new core, under construction ([TASK-314](https://linear.app/ophis-workgroup/issue/TASK-314)); it replaces the root `core/` at cutover. The Composer reads the root `core/team/` in place; its output text (`deliverable_file`) now follows the new core, so the old core and the repo's `tests/` no longer match it (CI runs neither; the cutover replaces them). Terms: root `CONTEXT.md`; diagram: root `README.md` › Core architecture (planned). Paths below are relative to `new/`; commands run from the repo root. Each module's docstring states its behaviour.

## Layout

| Path | What |
|---|---|
| `core/src/` | the layers, one package each: `adapters/`, `composer/`, `driver/`; `delegate` comes later. `repo.py`, a module outside the layers |
| `core/src/adapters/claude/` | Claude Code's Adapter, runtime `claude` |
| `core/config.toml` | the Composer's run keys (`runtime` among them) and `[roles.<role>]` tables, the Driver's `[adapters.<name>]` tables (the registry reads `[adapters]` once per process); which layer reads which keys, and this machine's settings in `~/.agent-pm/core-new.local.toml`: the file's header |
| `tests/` | `unit/`, `integration/`, `fake_adapter/` (the test-only runtime `fake`), `fake_claude.py` (a fake `claude` for the integration tests), `guards.py` |
| `tests/live/` | live checks, run by hand with the real HOME and `claude`, never in CI: `python3 new/tests/live/claude_headless.py` |

Imports go one way: delegate → composer or driver, driver → adapters, a module outside the layers → driver; outside `driver/`, only `driver.api`. `tests/guards.py` (`layer_imports`) enforces it, with the other rules in its docstring.

## Tests

```bash
python3 -m unittest discover -s new/tests/unit -t . -p "*_test.py"
python3 -m unittest discover -v -s new/tests/integration -t . -p "*_test.py"
```

CI (`.github/workflows/test.yml`) runs each in its own job. Never run them in one process with the repo's `tests/`: nothing here imports it or the old core (root `core/src/`) (`tests/guards.py` `old_core`).
