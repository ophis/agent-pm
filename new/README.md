# new/

The new core, under construction ([TASK-314](https://linear.app/ophis-workgroup/issue/TASK-314)); it replaces the root `core/` at cutover. Terms: root `CONTEXT.md`; diagram: root `README.md` › Core architecture (planned). Paths below are relative to `new/`; commands run from the repo root. Each module's docstring states its behaviour.

## Layout

| Path | What |
|---|---|
| `core/src/` | the layers, one package each: `adapters/`, `driver/`; `composer` and `delegate` come later |
| `core/config.toml` | the `[adapters.<name>]` tables; this machine's settings go in `~/.agent-pm/core-new.local.toml` (the file's header) |
| `tests/` | `unit/`, `integration/`, `fake_adapter/` (the test-only runtime `fake`), `guards.py` |

Imports go one way: delegate → composer or driver, driver → adapters, a module outside the layers → driver; outside `driver/`, only `driver.api`. `tests/guards.py` (`layer_imports`) enforces it, with the other rules in its docstring.

## Tests

```bash
python3 -m unittest discover -s new/tests/unit -t . -p "*_test.py"
python3 -m unittest discover -v -s new/tests/integration -t . -p "*_test.py"
```

CI (`.github/workflows/test.yml`) runs each in its own job. Never run them in one process with the repo's `tests/`: nothing here imports it or the old core (root `core/src/`) (`tests/guards.py` `old_core`).
