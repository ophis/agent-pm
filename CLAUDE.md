# CLAUDE.md

Agent pipeline on a Linear board (README). The core runs one role, on a task given or picked from its index; the orchestrator turns a Linear issue into that agent run's input, and its outcome into Linear changes. Their code: `new/README.md`.

## Commands

The plugin (`claude --plugin-dir`, `claude plugin validate`) and the tests (layout, commands): `new/README.md` › Layout, › Tests.

```bash
uvx ruff@0.16.9 check .                                   # lint; config: pyproject.toml
uvx ruff@0.16.9 format .                                  # format; CI pins the same version
PYTHONPATH=new/core/src:new/orchestrator/src python3 -m unittest discover -v -s .github -p "*_test.py"  # .github scripts' tests
```

CI (`.github/workflows/test.yml`) runs on Ubuntu, Python 3.11, for each PR and push to `main`: Comment budget, Ruff (`ruff check`, `ruff format --check`) and the test jobs; a test must pass on Linux too. Comment budget (`.github/comment_budget.py`) caps each directory's comment plus docstring characters over its own git-tracked `.py` files (not its subdirectories'), source and tests (`(^|/)tests/` or `_test.py`) separately, at 15% (source) or 10% (tests) of their code characters plus 500 characters: keep a comment only for a gotcha (≤3 lines).
The comment budget keeps comments short: meet it by cutting comments or docstrings, never by adding code to lower the ratio.

Operating: README › Operating, README › Attended runs. Without `--dry-run`, router and promote change real issues and start real agent runs. Python 3.11+ (`tomllib`); macOS's own `python3` is 3.9.

## Architecture

The core's and orchestrator's modules, entries and import rules: `new/README.md`.

- Identity: write-back and bounce act as the role's `account`, the rest as the harness account (README › Setup). Only Keychain service names travel in env, argv, logs and prompts, never keys. As the same macOS user, an agent run could read Keychain items: no enforced boundary.
- No move changes an assignee; every move to In Review (write-back, bounce or harness) also subscribes each `human_members` email.

## Roles, tasks, rules

- Config and test isolation: `new/README.md` › Layout.
- Core owns roles, tasks, methods, principles and templates (`new/core/team/`, `new/core/config.toml`); `new/orchestrator/config.toml` adds what core must not know, plus the overlay.
- Where a rule goes: every role → `new/core/team/principles.md`; every task of one role → `new/core/team/roles/<role>.md`; a document format → `new/core/team/templates/`; a method → `new/core/team/methods/`; one task (claiming, failure, hand-off, resume) → `new/core/team/tasks/<task>.md`; a Linear fact (comment text, state, title) → `writeback.py`, its per-role differences → `config.TASKS` data. Precedence: `new/core/team/guide.md`.
- Say each thing once: one home per rule or fact; elsewhere point to it, never restate.
- No em dashes (U+2014): use a colon, semicolon, comma or parentheses.
- Every task has a `## Resume` section; shared text: `agent_pm.composer.prompt.RESUME`, then the charter's Resume item, if any (engineer: **Resume** in `## Repo`).
- Add a task to a role: `new/core/team/tasks/<task>.md`, its line `` - `<task>` (`<tasks>/<task>.md`): … `` in the role's task index (format: `index` in `new/core/src/agent_pm/composer/teams.py`), its label in the `Tasks` group and `<task> = "<label id>"` in `[task_labels]`.

## Gotchas

- This file never loads in an agent run outside a trusted cwd (README › Install). An agent run reaches only its cwd, `<work_dir>/work/<ID>/` and the config's `read`/`write` dirs: give a task a new path there, or it stalls on a permission nobody can grant.
- `inputs_test.py` and `writeback_test.py` pin the input text and the Linear calls.
- Every task has exactly one `[agent-pm-progress:start]` line and no `budget` one, which start replaced (`new/tests/unit/composer/real_test.py` checks): without the start report the Delegate logs `missing progress mark: start`, write-back posts no start comment, and a build's `issues.build_cutoff` loses its `Build started` cutoff.
- Never name Linear in core prompts or skills.
- Skills and `new/core/config.toml` comments name a file by its path and a skill by its slash command (`/agent-pm:act-as`), never "the act-as skill" or `agent-pm:act-as`.
- Identify Linear entities by id, never name; a role's `account` (an email) is the exception.
- Linear's lists can lag a just-made state change: re-read an issue's state before acting on it (`linear.move` does).
- Tmux calls: `new/core/src/agent_pm/driver/tmux_host.py` (`new/README.md` › Layout). `/agent-pm:tmux`: removed, rebuilt by TASK-356.
- Keep `<work_dir>/logs/runs.jsonl` (README › Operating). The plists are installed as copies (README › Setup).
- A `[local_clones]` clone must not move: that breaks its worktrees. Run `git -C <new path> worktree repair`, then update the entry.

## Agent skills

### Domain docs

Single-context: root `CONTEXT.md` plus `docs/adr/`. See `docs/agents/domain.md`.
