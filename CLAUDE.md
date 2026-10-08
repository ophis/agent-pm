# CLAUDE.md

Agent pipeline on a Linear board (README): launchd runs `orchestrator/src/router.py`, which starts `run.py` for an issue it claims or resumes; `run.py` has `core/` (`core/CLAUDE.md`) run a role on one task and writes the outcome back to Linear.

## Commands

```bash
python3 -m unittest discover -s orchestrator/src/tests -p "*_test.py"   # no network, Keychain or Claude; -k <pattern> picks tests
python3 -m unittest discover -s core/src/tests -p "*_test.py"           # no network, Claude, tmux or osascript
python3 -m unittest discover -s core/skills/tmux/scripts -p "*_test.py"
claude plugin validate core && claude plugin validate .                 # core/CLAUDE.md at the plugin root: a warning
claude --plugin-dir core                                                # this checkout's plugin as agent-pm@inline; /reload-plugins after edits
```

Python 3.11+ (`tomllib`); macOS's `python3` is 3.9, so launchd uses `/opt/homebrew/bin/python3`. Without `--dry-run`, router and promote change real issues and start real agent runs (README › Operating).

A change to `tui_claude.py`, `drive.py`, `workers.py` or the tmux skill also needs a live `dummy-tester echo` run, tui and headless (the unit tests fake tmux), on a private tmux server (`core/src/tui_claude.py`'s docstring: Live check); say in the PR what ran and what it showed.

## Modules

Before changing an `orchestrator/src/` module or what it does, read its docstring:

- `router.py`: a tick's steps, the usage gate, router state, `--brake`.
- `run.py`: an agent run's outer, attended and inner steps.
- `attended.py`: TUI session names; driver sessions and `max_runs` slots.
- `issues.py`: reading an issue.
- `inputs.py`: an agent run's input text.
- `target.py`: the target repo, pre-check, branch, checkout and clone.
- `writeback.py`: what an agent run writes to Linear; the bounce.
- `sessions.py`: the `Run <sid>` session comments.
- `promote.py`: Handoff to the next role.
- `prune.py`: cleanup of finished issues.
- `config.py`: paths, `work_dir` rules, config checks, the overlay, `TASKS`.
- `linear.py`: the Linear client; what every move to In Review does.

## Rules

- Say each thing once: one home per rule or fact; elsewhere point to it, never restate.
- No em dashes (—): use a colon, semicolon, comma or parentheses.
- Where a rule goes (agent runs load this file only from a trusted cwd: README › Install): every role → `core/team/principles.md`; every task of one role → `core/team/roles/<role>.md`; one task → `core/team/tasks/<task>.md`; a document format → `core/team/templates/`; a method → `core/team/methods/`; a Linear fact (comment text, state, title) → `writeback.py` / `config.TASKS`. Precedence: `core/team/guide.md`.
- Add a task to a role: `core/team/tasks/<task>.md` (its format: `core/src/compose.py`'s docstring), `[roles.<role>.tasks.<task>]` in `core/config.toml`, the task in `config.TASKS`, its label in the `Tasks` group and `<task> = "<label id>"` in `[task_labels]`.
- Imports: `config` puts `core/src` first on `sys.path`, so no `orchestrator/src/` module is named `clients`, `compose`, `drive`, `repo`, `report` or `tui_claude`, `core/src` gains no `config` or `linear` module, and a module importing a core module imports `config` first.
- Identity: accounts per README › Setup; write-back and bounce act as the role's `account`. Only Keychain service names travel in env, argv, logs and prompts, never keys. An agent run, as the same macOS user, could read Keychain items: no enforced boundary.
- Identify Linear entities by id; a role's `account` (an email) is the one exception.
- A test module reading the repo's config imports `hermetic` first (`orchestrator/src/tests/hermetic.py`, `core/src/tests/hermetic.py`).

## Gotchas

- An agent run reaches only its cwd, `<work_dir>/work/<ID>/` and the config's `read`/`write` dirs: put a path a task needs there, or the run stalls on a permission nobody can grant.
- `inputs_test.py` and `writeback_test.py` pin the input text and the Linear calls; `run_test.py` pins the tmux argv.
- Each pipeline task (deep-research, light-research, product-design, build, light-build) has exactly one `[agent-pm-progress:start]` line (`compose_test.py` checks): without it write-back posts no start comment, and a build's `issues.build_cutoff` loses its `Build started` cutoff.
- Code targets a tmux session only by `=<name>` (why: `core/skills/tmux/SKILL.md` › Gotchas).
- Linear's lists can lag a just-made state change; re-read an issue's state before acting on it.
- Keep `<work_dir>/logs` (README › Operating).
- A `[local_clones]` clone that moves breaks its worktrees: run `git -C <new path> worktree repair`, then update the entry.

## Agent skills

### Domain docs

Single-context: root `CONTEXT.md` plus `docs/adr/`. See `docs/agents/domain.md`.
