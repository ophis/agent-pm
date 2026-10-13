# CLAUDE.md

## Gotchas

- This file never loads in an agent run outside a trusted cwd (README › Install). An agent run reaches only its cwd, `<work_dir>/work/<ID>/` and the config's `read`/`write` dirs: give a task a new path there, or it stalls on a permission nobody can grant.
- `inputs_test.py` and `writeback_test.py` pin the input text and the Linear calls.
- Every task has exactly one `[agent-pm-progress:start]` line and no `budget` one, which start replaced (`delegate/tests/unit/composer/real_test.py` checks): without the start report the Delegate logs `missing progress mark: start`, write-back posts no start comment, and a build's `issues.build_cutoff` loses its `Build started` cutoff.
- Never name Linear in core prompts or skills.
- Skills and `delegate/config.toml` comments name a file by its path and a skill by its slash command (`/agent-pm:act-as`), never "the act-as skill" or `agent-pm:act-as`.
- Identify Linear entities by id, never name; a role's `account` (an email) is the exception.
- Linear's lists can lag a just-made state change: re-read an issue's state before acting on it (`linear.move` does).
- Tmux calls: `session/src/agent_pm/session/hosts/tmux/tmux_host.py` (README › Layout). `/agent-pm:tmux`: removed, rebuilt by TASK-356.
- Keep `<work_dir>/logs/runs.jsonl` (README › Operating).
- A `[local_clones]` clone must not move: that breaks its worktrees. Run `git -C <new path> worktree repair`, then update the entry.

## Tests

- A test of `agent_pm.<sub>` goes in its project's `tests/unit/` or `tests/integration/`; a shared helper with its owner (`session/tests/live_tmux.py`); each project's `tests/case.py` holds its own `Case` copy and only the helpers its tests use; only repo-wide checks in the root `tests/` (`case.py`, `guards.py`).
- Tests follow the layer rule (utils < session < delegate < linear-orchestrator < manager): a project's tests import their own project, lower ones (source or tests) and the stdlib, never the root `tests` (`tests/guards.py` `test_layers`).
- Imports: the repo root on `sys.path` (`-t .`) makes each project's `tests/` the package `<project>.tests` (e.g. `session.tests.fakes.host`), the root's `tests`; `linear-orchestrator`'s hyphen leaves its tests only relative imports. No two test packages share a dotted name, so one process may run them all.
- Run (PYTHONPATH: README › Tests):

```bash
python3 -m unittest discover -s session/tests/unit -t . -p "*_test.py"   # one project's unit tests; any <project>/tests/<kind>
python3 .github/integration_jobs.py JOB   # one CI integration job, or --check
```

### Domain docs

Single-context: root `CONTEXT.md` plus `docs/adr/`. See `docs/agents/domain.md`.
