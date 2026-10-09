# CLAUDE.md

Agent pipeline on a Linear board (README). `core/` runs one role, on a task given or picked from its index (`core/CLAUDE.md`); `orchestrator/src/` turns a Linear issue into that agent run's input, and its outcome into Linear changes.

## Commands

```bash
python3 -m unittest discover -s orchestrator/src/tests -p "*_test.py"
python3 -m unittest discover -s core/src/tests -p "*_test.py"
python3 -m unittest discover -s core/skills/tmux/scripts -p "*_test.py"
python3 -m unittest discover -s core/src/tests/integration -p "*_integration_test.py"
python3 -m unittest discover -s core/skills/tmux/scripts/integration -p "*_integration_test.py"
claude --plugin-dir core                                  # this checkout's plugin as agent-pm@inline; /reload-plugins after edits
claude plugin validate core && claude plugin validate .   # plugin and marketplace manifests; core/CLAUDE.md at the plugin root warns
```

CI (`.github/workflows/test.yml`) runs on Ubuntu, Python 3.11, for each PR and push to `main`: Unit tests (the three suites) and Integration tests (each suite's `integration/`; installs tmux); a test must pass on Linux too.

Integration tests (real processes end to end) go in a suite's `integration/` as `<x>_integration_test.py`, with no `__init__.py` (the unit discovery then skips them). A new such dir gets a step in the Integration tests job (guarded like the unit job's later steps) and a line in Commands.

Operating: README › Operating, README › Attended runs. Without `--dry-run`, router and promote change real issues and start real agent runs. Python 3.11+ (`tomllib`); macOS's own `python3` is 3.9.

A change to `tui_claude.py`, `drive.py`, `workers.py` or the tmux skill also needs a live `dummy-tester echo` run, tui and headless (the tests fake `claude` and never drive iTerm2), its tmux isolated as core/CLAUDE.md › Rules says (a live check); say in the PR what ran and what it showed.

## Architecture

- `router.py`, a tick: hours check → the router lock → live tmux sessions vs `max_runs` → Recover dead In Progress agent runs → usage gate (5-hour usage ≥ 90% or a weekly limit full → skip) → resume, or claim a ready Todo issue → `outer`, in-process (an exception is logged; the tick goes on). At most one agent run per tick. `--issue ID`: README › Operating.
- The router lock, `<work_dir>/logs/router.lock` (flock, held until that router is done), keeps one router at a time, so a claim's driver session is up before another router reads `tmux ls`; held by another router → one `skip` event, nothing else. `--issue` takes it too; `--dry-run` and `--brake` never.
- `router.outer`: validate → read the issue → a role of `config.TASKS` kind `build` (engineer): `target.check` (Invalid on a new agent run → `writeback.bounce`, no agent run) → `inputs.gather` + `render` → `drive.detach`: tmux driver session `agent-pm-<role>-<ID>` running `run.py --input <text>` (the argv through the handover file), cwd `<work_dir>/work/<ID>/`. Any failure starts nothing. `--tui` passes the inner `--opener` and only the user's `--split`/`--split-from`/`--events`, never a derived `--split-from`.
- `run.py`, the inner: `attended.close` → `drive.plan` (`config.layers`, the overlay) + `drive.start` (sinks: terminal, `writeback.sink`; `begun` posts the session comment) → `end` event in `orchestrator.jsonl` → `writeback.finish`. No valid outcome → stays In Progress; Recover resumes it. `config.run_config` mirrors those layers for validation (`runnable`, `docs`).
- `attended.py` owns the TUI session name `<role>-<ID>-<sid[:8]>`: `prefix` its only builder, `ISSUE_TUI` with `drive.TUI_SESSION` its only matcher. An issue's TUI sessions are the live tmux sessions so named, nothing recorded; the inner and prune `close` them (no resume beside an old `claude`). The driver session is the issue's live agent run (`router.live_sessions`: no second one starts) and holds a `max_runs` slot while its driver runs; a TUI session is neither.
- Write-back: `<work_dir>/work/<ID>/writeback.json` ledgers each step per session, so a resume repeats none; a failed step leaves the issue In Progress.
- `sessions.py`: one `Run <sid> · …` comment per session, posted with `harness_key` (`isMe` marks it); a resume edits it, a new claim adds one. A write is one try; a failure only logs `registry-error`. `issues.read_issue` and promote drop these comments. One stuck at `running` with no `agent-pm-<role>-<ID>` tmux session was killed: `tmux ls` is the truth.
- `promote.py`: Handoff → the next role's Todo issue, its id hashed from source, role and handoff time (reruns are idempotent); then `prune.py` (its docstring). launchd's PATH lacks tmux and claude: subprocesses get `config.PATH`.
- `config.WORK_DIR` is `<work_dir>` (`orchestrator/config.toml`'s `work_dir`). A `trusted_dirs` entry that is or contains it stops router, run and promote: git in a workdir must not see agent-pm, nor its `CLAUDE.md` and `.claude/` reach the run.
- Imports: `linear` imports `config`, never the reverse. `config` puts `core/src` first on `sys.path`: no `orchestrator/src/` module may be named `clients`, `compose`, `drive`, `repo`, `report` or `tui_claude`; `core/src` gains no `config` or `linear` module; a module importing a core module imports `config` first.
- Identity: write-back and bounce act as the role's `account`, the rest as the harness account (README › Setup). Only Keychain service names travel in env, argv, logs and prompts, never keys. As the same macOS user, an agent run could read Keychain items: no enforced boundary.
- No move changes an assignee; every move to In Review (write-back, bounce or harness) also subscribes each `human_members` email.
- Logs: `linear.log` is the only writer of `<work_dir>/logs/orchestrator.jsonl` (README › Operating).
- State between ticks: only `<work_dir>/logs/runs.jsonl` (Recover resumes a session only while its `role` is the assignee's; the session keeps its task) and Linear history, never session comments. A resume also needs `writeback.json`, `run.jsonl` and the transcript (`config.transcript`).

## Roles, tasks, rules

- Config: each `config.toml` documents every key and has a local file merged over it (its header); `repo.read_config` is the only loader, and validation runs on the merge. An invalid config stops router, run and promote (`config.runnable`). Tests never read a real local file: a test module reading the repo's config (every orchestrator one) imports `hermetic` first; a test writes its own local file under `hermetic.home`.
- Core owns roles, tasks, methods, principles and templates (`core/team/`, `core/config.toml`); `orchestrator/config.toml` adds what core must not know, plus the overlay.
- Where a rule goes: every role → `core/team/principles.md`; every task of one role → `core/team/roles/<role>.md`; a document format → `core/team/templates/`; a method → `core/team/methods/`; one task (claiming, failure, hand-off, resume) → `core/team/tasks/<task>.md`; a Linear fact (comment text, state, title) → `writeback.py`, its per-role differences → `config.TASKS` data. Precedence: `core/team/guide.md`.
- Say each thing once: one home per rule or fact; elsewhere point to it, never restate.
- No em dashes (U+2014): use a colon, semicolon, comma or parentheses.
- Every task has a `## Resume` section; shared text: `compose.RESUME`, then the charter's `## Resume`, if any.
- Add a task to a role: `core/team/tasks/<task>.md`, its line in the role's task index (`core/CLAUDE.md` › Rules), its label in the `Tasks` group and `<task> = "<label id>"` in `[task_labels]`.

## Gotchas

- This file never loads in an agent run outside a trusted cwd (README › Install; core/CLAUDE.md › An agent run's command). An agent run reaches only its cwd, `<work_dir>/work/<ID>/` and the config's `read`/`write` dirs: give a task a new path there, or it stalls on a permission nobody can grant.
- `inputs_test.py` and `writeback_test.py` pin the input text and the Linear calls; `router_test.py` pins the tmux argv.
- Every task has exactly one `[agent-pm-progress:start]` line and no `budget` one, which start replaced (`compose_test.py` checks): without the start report `drive.py` logs `missing progress mark: start`, write-back posts no start comment, and a build's `issues.build_cutoff` loses its `Build started` cutoff.
- Never name Linear in core prompts or skills; `compose_test.py` fails on it.
- Identify Linear entities by id, never name; a role's `account` (an email) is the exception.
- Linear's lists can lag a just-made state change: re-read an issue's state before acting on it (`linear.move` does).
- Tmux targets: `core/skills/tmux/SKILL.md` › Gotchas. The code targets only with `=<name>`: the router reads `list-sessions` and names via `new-session -s`; `attended.close` kills by exact target (`tui_claude.kill`).
- Keep `<work_dir>/logs/runs.jsonl` (README › Operating). The plists are installed as copies (README › Setup).
- A `[local_clones]` clone must not move: that breaks its worktrees. Run `git -C <new path> worktree repair`, then update the entry.

## Agent skills

### Domain docs

Single-context: root `CONTEXT.md` plus `docs/adr/`. See `docs/agents/domain.md`.
