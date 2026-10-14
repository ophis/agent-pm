# AGENTS.md

## Gotchas

- This file never loads in an agent run outside a trusted cwd (README › Install). An agent run reaches only its cwd, `<work_dir>/work/<ID>/` and the config's `read`/`write` dirs: give a task a new path there, or it stalls on a permission nobody can grant.
- Input rendering and write-back changes: `linear-orchestrator/tests/orchestrator_tests/unit/engine/inputs_test.py` and `linear-orchestrator/tests/orchestrator_tests/unit/engine/writeback_test.py` pin input text and Linear calls.
- Every task has exactly one `[agent-pm-progress:start]` line and no `budget` one, which start replaced (`delegate/tests/unit/team_test.py` checks): without the start report the Delegate logs `missing progress mark: start`, write-back posts no start comment, and a build's `issues.build_cutoff` loses its `Build started` cutoff.
- Never name Linear in the team text (`delegate/src/agent_pm/delegate/team/`) or skills.
- Skills and `config.toml` comments name a file by its path and a skill by its slash command (`/agent-pm:act-as`), never "the act-as skill" or `agent-pm:act-as`.
- A layer's packaged `config.toml` (`<project>/src/agent_pm/<package>/config.toml`) holds only its keys, each a real line (a local-only one its empty default, its comment saying what empty means); a `# local:` line only shows a value for `~/.agent-pm/config.local.toml` (`tests/guard_checks/configuration.py` `explicit_keys`). Layer sections and merge rules: `utils/src/agent_pm/utils/config_file.py`'s docstring.
- Identify Linear entities by id, never name; a role's `account` (an email) is the exception.
- Linear's lists can lag a just-made state change: re-read an issue's state before acting on it (`linear-orchestrator/src/agent_pm/linear_orchestrator/engine/ops.py:move` does).
- Tmux implementation: `session/src/agent_pm/session/hosts/tmux/` (`TmuxHost` and the `Tmux` it holds: `host.py`). `/agent-pm:tmux`: removed, rebuilt by TASK-356.
- The session's and delegate's public interfaces are frozen: `tests/guard_checks/exports.py` `session_interface_snapshot` and `delegate_interface_snapshot` (who may change `tests/session_interface.json` and `tests/delegate_interface.json`: their docstrings).
- Keep `<work_dir>/logs/runs.jsonl` (README › Operating).
- A `[local_clones]` clone must not move: that breaks its worktrees. Run `git -C <new path> worktree repair`, then update the entry.

## Import rules

Layer contracts live in `pyproject.toml` and run with `lint-imports`: manager > linear_orchestrator > delegate > session > utils. Manager imports only itself and utils; it reaches other layers through commands. Cross-layer imports use the lower layer's public package. Orchestration reaches session only through Delegate: its contract ignores no import. Project tests may import their project and lower layers, never the root `tests` package or a higher project.

Session and Delegate internal units use the longest matching package; their dependency rules and shared-module purity are declared in `pyproject.toml`'s unit contracts. Session owns no config reader, reply-protocol tag or team text; `tests/guard_checks/` enforces those names and literal dynamic imports.

Repository design rules live in `tests/guard_checks/`, one module per concern: namespace and subsystem placement, unit test placement, shared types, client-name ownership (`client_names`), configuration and import side effects. A rule's exceptions and a layer's constants live in `tests/guard_data/<package>.py` of the layer owning the path or package they name. A helper used by one file stays there as a private function; otherwise, a helper independent of its layer's own classes goes in `utils/`. Shared data types live in their layer's types module; standalone scripts keep theirs local. The `standalone_imports` guard owns their inventory and enforces standard-library-only imports.

Python suppressions require a matching `NOQA_ALLOWED` entry in its layer's `tests/guard_data/<package>.py`; Pylint disables are paired with block-scoped enables.

Vulture scans only the five `src` trees, so code only tests use is dead: delete it. A name it cannot see used goes in `.github/vulture_whitelist.py` (rules: its docstring).

## Domain docs

Single-context: root `CONTEXT.md`. `docs/` (ADRs, agent notes) is gitignored: local only.
