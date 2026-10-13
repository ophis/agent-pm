# AGENTS.md

## Gotchas

- This file never loads in an agent run outside a trusted cwd (README › Install). An agent run reaches only its cwd, `<work_dir>/work/<ID>/` and the config's `read`/`write` dirs: give a task a new path there, or it stalls on a permission nobody can grant.
- `inputs_test.py` and `writeback_test.py` pin the input text and the Linear calls.
- Every task has exactly one `[agent-pm-progress:start]` line and no `budget` one, which start replaced (`delegate/tests/unit/composer/real_test.py` checks): without the start report the Delegate logs `missing progress mark: start`, write-back posts no start comment, and a build's `issues.build_cutoff` loses its `Build started` cutoff.
- Never name Linear in the team text (`delegate/src/agent_pm/delegate/team/`) or skills.
- Skills and `config.toml` comments name a file by its path and a skill by its slash command (`/agent-pm:act-as`), never "the act-as skill" or `agent-pm:act-as`.
- A project's `config.toml` holds only its keys, each a real line (a local-only one its empty default, its comment saying what empty means); a `# local:` line only shows a value for `~/.agent-pm/config.local.toml` (`tests/guards.py` `explicit_keys`). Levels, merge and ownership: `utils/src/agent_pm/utils/config_file.py`'s docstring.
- Identify Linear entities by id, never name; a role's `account` (an email) is the exception.
- Linear's lists can lag a just-made state change: re-read an issue's state before acting on it (`linear.move` does).
- Tmux calls: `session/src/agent_pm/session/hosts/tmux/tmux_host.py`. `/agent-pm:tmux`: removed, rebuilt by TASK-356.
- Keep `<work_dir>/logs/runs.jsonl` (README › Operating).
- A `[local_clones]` clone must not move: that breaks its worktrees. Run `git -C <new path> worktree repair`, then update the entry.

## Import rules

Layer contracts live in `pyproject.toml` and run with `lint-imports`: manager > linear_orchestrator > delegate > session > utils. Cross-layer imports use the lower layer's public package. Layer skipping remains permitted until TASK-371 and TASK-372. Project tests may import their project and lower layers, never the root `tests` package or a higher project.

Repository design rules live in `tests/guards.py`: namespace and subsystem placement, shared types, runtime ownership, configuration and import side effects. Shared classes used across their layer live in a `types.py` module; type modules import no higher layer. Standalone hooks use only the standard library.

## Tests

Tests of the whole repository's source go in the root `tests/`; tests of a `.github/` script go next to it in `.github/`. Project tests go in `<project>/tests/unit/` or `integration/`, with shared helpers in non-test modules beside their owner. The orchestrator uses `linear-orchestrator/tests/orchestrator_tests/{unit,integration,case.py}` so imports have a normal Python package name; other projects use `tests/case.py` for their `Case`. Never import a `*_test` module or patch shipped code; construct fresh dependencies and patch only standard-library boundaries or lower-layer fakes.

Use the Mac's own `python3` (3.14) locally; GitHub CI uses Python 3.11 on Ubuntu. From the repository root, set `PYTHONPATH` as in README › Tests, then run only the affected tests while working and one full pass at completion:

```bash
python3 .github/run_tests.py 'Unit session' session/tests/unit
python3 .github/run_tests.py 'Unit orchestrator' linear-orchestrator/tests/orchestrator_tests/unit
python3 .github/run_tests.py Unit --unit
python3 .github/run_tests.py 'Repository guards' tests/unit
python3 .github/run_tests.py '.github scripts' .github
python3 .github/integration_jobs.py 1
python3 .github/integration_jobs.py --check
```

CI's Lint job runs comment budget, Ruff, Pyright, import-linter, vulture, pylint's cyclic-import and duplicate-code checks, actionlint's native workflow checks, typos and `python3 -m tests.checks` (repository design, 500-line file limit and test-boundary AST checks). Unit runs every unit suite and `.github/` tests through `.github/run_tests.py`; Integration distributes individual tests across eight numbered shards using `.github/integration_durations.json`; new tests use the median recorded duration. Set `RUN_TESTS_DURATIONS` to a file outside the checkout to collect timings. Test jobs make source read-only and fail on a changed Git tree. The aggregate `CI` job requires every check and test job to succeed.

## Domain docs

Single-context: root `CONTEXT.md`. `docs/` (ADRs, agent notes) is gitignored: local only.
