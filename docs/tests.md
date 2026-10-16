# Tests

```bash
export PYTHONPATH=.:.github:delegate/src:session/src:utils/src:manager/src:linear-orchestrator/src:linear-orchestrator/tests   # from the repo root
```

Lint (tool versions pinned in `.github/workflows/test.yml`):

```bash
python3 .github/comment_budget.py
ruff check --no-cache
ruff format --check --no-cache
pyright
lint-imports --no-cache
pylint --disable=all --enable=cyclic-import,duplicate-code manager/src linear-orchestrator/src delegate/src session/src utils/src
vulture
python3 -m tests.checks
python3 .github/run_tests.py "Repository integration" tests/integration
python3 .github/integration_jobs.py --check
actionlint -shellcheck= -pyflakes=
typos --config pyproject.toml
```

| Suite | Command |
|---|---|
| one project's unit tests | `python3 .github/run_tests.py "Session unit" session/tests/unit` |
| all unit tests | `python3 .github/run_tests.py Unit --unit` |
| integration shard 1–8 | `python3 .github/integration_jobs.py <N>` |

Run tests only through these entries; they sandbox each run.

A PR's added and changed `*/src` lines need unit coverage (lines and branches) of at least `THRESHOLD` in `.github/coverage_gate.py` (CI job `Coverage`). With coverage.py installed (version: `COVERAGE_VERSION` in `.github/workflows/test.yml`):

```bash
RUN_TESTS_COVERAGE=/tmp/coverage-data python3 .github/run_tests.py Unit --unit
python3 .github/coverage_gate.py /tmp/coverage-data origin/main
```

`# pragma: no cover` is allowed only in the header (`def` through its colon) of a function that only integration tests run, registered with a reason in `NO_COVER_ALLOWED` of its layer's `tests/guard_data/<package>.py` (check: `tests/guard_checks/suppressions.py` `coverage_pragmas`).

| | Unit | Integration |
|---|---|---|
| Tests | pure logic, with fakes and recorded output | real tools in `utils/tests/sandbox.py` |
| Limits | no processes, no sockets, writes only in the runner's temp dir | tmux only on `session/tests/live_tmux.py` servers |
