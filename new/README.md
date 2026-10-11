# new/

The new core, under construction ([TASK-314](https://linear.app/ophis-workgroup/issue/TASK-314)); it replaces the root `core/` at cutover. Terms: root `CONTEXT.md`; diagram: root `README.md` › Core architecture (planned). Paths below are relative to `new/`; commands run from the repo root. Each module's docstring states its behaviour.

## Layout

| Path | What |
|---|---|
| `core/src/` | the layers, one package each: `adapters/`, `driver/`; `composer` and `delegate` come later |
| `core/config.toml` | the `[adapters.<name>]` tables; this machine's settings go in `~/.agent-pm/core-new.local.toml` (the file's header) |
| `tests/` | `unit/`, `integration/`, `fake_adapter/` (the test-only runtime `fake`), `live/`, `guards.py` |

Imports go one way: delegate → composer or driver, driver → adapters, a module outside the layers → driver; outside `driver/`, only `driver.api`. `tests/guards.py` (`layer_imports`) enforces it, with the other rules in its docstring.

## Driver queries

`driver.api.start(..., sid=)` accepts a caller's new session id; it must agree with `spec.sid` and `resume` when supplied. `usage(runtime)` returns `Usage`, with unavailable fields set to `None`. `session_info(runtime, sid, cwd)` returns `SessionInfo`, including `resumable` (`yes/no/unknown`), `last_active` and `resume_command`. Query with the session cwd, which may differ from the workdir.

Recovery belongs to each Adapter; its runtime module documents the store and probe. `run.jsonl` does not authorize recovery.

## Tests

```bash
python3 -m unittest discover -s new/tests/unit -t . -p "*_test.py"
python3 -m unittest discover -v -s new/tests/integration -t . -p "*_test.py"
```

CI (`.github/workflows/test.yml`) runs each in its own job. Never run them in one process with the repo's `tests/`: nothing here imports it or the old core (root `core/src/`) (`tests/guards.py` `old_core`).

The real headless check runs separately, uses installed runtime settings and writes its session binding. Its JSON output includes every event, reply, caller id, native id and exit status. Pass a clean workdir outside the Adapter store; do not invoke it as a unittest module.

```bash
python3 new/tests/live/headless.py --runtime codex --workdir /absolute/path/to/live-work --sid caller-id
```
