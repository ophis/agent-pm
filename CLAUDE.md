# CLAUDE.md

Agent pipeline on a Linear board: launchd runs `router.py`, which starts one `run.py` per Todo issue assigned to a role account (researcher → pm → engineer; project = product, assignee = stage). An agent run is a core role doing one core task: the issue's `Tasks` label's (via `[task_labels]`), else the role's default. `core/` knows nothing of Linear (`core/CLAUDE.md`); `orchestrator/src/` turns an issue into the run's input and its outcome into Linear changes.

## Commands

```bash
python3 -m unittest discover -s orchestrator/src/tests -p "*_test.py"      # orchestrator tests; no network, Keychain or Claude
python3 -m unittest discover -s orchestrator/src/tests -p "*_test.py" -k attempt   # tests whose name matches
python3 -m unittest discover -s core/src/tests -p "*_test.py"     # core tests
python3 -m unittest discover -s core/skills/tmux/scripts -p "*_test.py"           # tmux skill tests
claude --plugin-dir core                                          # this checkout's agent-pm plugin; /reload-plugins after edits
claude plugin validate core && claude plugin validate .           # plugin and marketplace manifests (core/CLAUDE.md at the plugin root: a warning)
```

Operating commands: README › Operating, README › Attended runs. Python 3.11+ (`tomllib`; macOS's `python3` is 3.9, so launchd uses `/opt/homebrew/bin/python3`). Without `--dry-run`, router and promote change real issues and start real agent runs.

## Architecture

- `router.py` (every 30 min): live tmux sessions vs `max_runs` → Recover dead In Progress agent runs (not ones with a live session) → usage gate → resume or claim → `run.py`; at most one agent run per tick, for a role below `max_runs`. Claims only ready Todo issues (README › Using the board), changing their state, never the assignee. `task_for` maps the `Tasks` label's id to the task (a bad label: README › Task labels). `--tui` (by hand): `run.py --runner tui`. `--brake`: deep-research's second-round gate (README › Operating).
- `run.py` outer (in the tick): validate → read the issue → `build`, `light-build`: `target.check` (Invalid on a new agent run → `writeback.bounce`, no agent run) → `inputs.gather` + `render` → `<work_dir>/work/<ID>/input.md` → tmux `agent-pm-<role>-<ID>`, cwd `<work_dir>/work/<ID>/`. Any failure starts nothing. `--runner tui` first checks, exit 2 on a failure: the pane (`attended.layout`; `tui_claude.anchor`: `--beside`'s pane, else the caller's tmux session's when a terminal shows it, else `$ITERM_SESSION_ID`'s; none → `attended.Bad`), `--events` (`tui_claude.events_file`), the name (`attended.prefix`). `layout` returns the `drive.Layout` with the caller's opener (`tui_claude.opener`). The driver session always starts detached on the caller's tmux server, with `-e ITERM_SESSION_ID=…` (tmux's global copy may be stale); the inner gets `--opener` and only the user's `--split`/`--beside`/`--events`, never a derived `--beside`; `tui_claude.start` places the pane (`core/CLAUDE.md`).
- `run.py --issue ID --tui`: pane and `--events` checks → refuse a live agent run (`router.live_sessions`) → `router.Board.take` (no hours, `max_runs` or usage gate) → `router.start_line` into `runs.log` → outer, `--runner tui`.
- `run.py` inner (in tmux): `attended.close` → `drive.plan` (`config.layers(root)`, the overlay; cwd default the workdir, passed explicitly) + `drive.start` (sinks: terminal, project log, `writeback.sink`; writes `<work_dir>/work/<ID>/run.json`; `begun` posts the session comment) → `end` lines in `<work_dir>/logs/runs.log` and the project log → `writeback.finish`. No valid outcome → stays In Progress; Recover resumes it. `config.run_config` mirrors the layers for validation (`runnable`, `docs`).
- `attended.py` owns the TUI session name `<role>-<ID>-<sid[:8]>`: `prefix` its only builder, `ISSUE_TUI` with `drive.TUI_SESSION` its only matcher. An issue's TUI sessions are the live tmux sessions so named (`sessions`, `issues`), nothing recorded; the inner `close`s them before every agent run (no resume beside an old `claude`), as does prune, both wording its `Closed` results. A driver session holds the lock and a `max_runs` slot only while its driver runs (`tmux kill-session` → SIGHUP → exit 129, TUI session killed, Recover resumes); a TUI session neither.
- `issues.py` reads an issue; `target.py` resolves its repo (`Repo:` line, else `[project_repos]`), runs the engineering pre-check, names the branch and, via `with_clone`, sets the path the input's `Repo:` names (the issue's existing checkout, else `[local_clones]`); `target.checkout` is that checkout: core's `repo.checkout` with the issue id as slug, the `--name` the input's `Checkout:` line passes; `inputs.py` builds the input text per task kind, reading linked docs via `gh api`.
- `writeback.py`, as the role account: `start` mark and outcome → Linear comments, title, attachment, state; other marks post once per text as `Progress (<name>): <text>`. Per-task differences (title prefixes, write-back texts) are `config.TASKS` data. `<work_dir>/work/<ID>/writeback.json` ledgers each step per session (a resume repeats none); a failed step leaves the issue In Progress.
- `sessions.py`: one `Run <sid> · …` comment per session, posted in-process by `run.py` with `harness_key`, not the role's key, so `isMe` is the harness account. Promote leaves them out of `## Comments`; `issues.read_issue` drops them.
- `promote.py` (every 5 min): Handoff → a Todo issue for the next role in the same project, its id hashed from source, role and handoff time (idempotent reruns). Then `prune.py`, for issues finished ≥ 24 h ago (a live TUI session alone makes one a candidate): `attended.close`, the cleanup of README › Using the board › Finish (no git on a worktree; refs only in its local clone), archiving pm and engineer ones (hence engineering's `issueUnarchive`). `promote.main` first sets `config.PATH` (launchd's lacks tmux).
- `config.py`: paths (`WORK_DIR`, written `<work_dir>`: README › Files; `RUNS_DIR`/`LOGS_DIR`: its `work`/`logs`), validation, core config with the overlay, `TASKS`. `linear.py`: client (Keychain-keyed GraphQL), lookups of the config's users, team and task labels, writes `comment`, `subscribe`, `comment_and_move`, `move` (re-reads the state; moves only from the expected one), `HISTORY`/`last_move`, helpers `parse_time`, `stamp`, `append` (a timestamped line), `log`, `one_line`. `repo.SHORT` and `err_text` serve the orchestrator too.
- Imports: `linear` imports `config`, never the reverse. `config` puts `core/src` first on `sys.path`: no `orchestrator/src/` module may be named `clients`, `compose`, `drive`, `repo`, `report` or `tui_claude`; `core/src` gains no `config` or `linear` module; a module importing a core module imports `config` first.
- Identity: the orchestrator acts as the harness account `frank.agent.w@gmail.com`; write-back and bounce as the role's `account` (`key`). Only Keychain service names travel in env, argv, logs and prompts, never keys. An agent run gets no Linear key, but as the same macOS user could read Keychain items: no enforced boundary.
- Every move to In Review (write-back, bounce or harness) subscribes each `human_members` email and keeps the assignee.
- Router state between ticks: only `<work_dir>/logs/runs.log` (resume and Recover take the task from its `task=`, never labels) and Linear history, never session comments. A resumed agent run also reads `<work_dir>/work/<ID>/writeback.json` and `run.json` (cwd, settings) and needs `~/.claude/projects/<escaped cwd>/<sid>.jsonl` (`config.transcript`: `run.json`'s cwd, else the workdir): moving `<work_dir>` or a run's cwd orphans sessions.

## Roles, tasks, rules

- Config: each committed `config.toml` has a local file merged over it, `~/.agent-pm/core.local.toml` and `~/.agent-pm/orchestrator.local.toml`; `config.toml` documents every key, marking with commented `# local:` lines those the local file sets (README › Core configuration); `repo.read_config` is the one loader, and validation runs on the merge. Tests never read a real local file: a test module reading the repo's config (in `orchestrator/src/tests`, every one: `config` reads it on import) imports `hermetic` first, which hides them and merges `core/src/tests/core.local.fixture.toml` over `core/config.toml`; a test writes its own under `hermetic.home`.
- Core owns roles, tasks, methods, principles and templates: `core/team/` (text), `core/config.toml` and `~/.agent-pm/core.local.toml` (keys: README › Core configuration). A role's default task is its `default_task`.
- The orchestrator config adds what core must not know (which key goes in which file: README › Orchestrator configuration); missing required keys stop the caller, naming them and the example. `[core]`, the overlay: core run keys for the orchestrator's agent runs, layered after `core/config.toml`'s `[clients.claude]` (`config.overlay()` fills `{{root}}`). An invalid or inconsistent config stops router, run and promote (`config.runnable`; it also checks the `[local_clones]` paths).
- Where a rule goes: every role → `core/team/principles.md`; every task of one role → `core/team/roles/<role>.md`; a document format → `core/team/templates/`; a method → `core/team/methods/`; one task (claiming, failure, hand-off, resume) → `core/team/tasks/<task>.md`; a Linear fact (comment text, state, title) → `writeback.py` / `config.TASKS`. One rule, one place. Precedence: principles > charter > task.
- Every task has a `## Resume` section; shared text: `compose.RESUME`.

## Gotchas

- Agent runs get `--strict-mcp-config` (`core/config.toml`'s `[clients.claude]`) and `--setting-sources user` (the driver's; a trusted cwd adds project settings: README › An agent run's command): this file and project settings never load in them. Their `--settings` turns the `agent-pm` plugin off (`clients/claude.py`); other user plugins load. An agent run reaches only its cwd, `<work_dir>/work/<ID>/` and the config's `read`/`write` dirs; give a task a new path there, or it stalls on a permission nobody can grant.
- `inputs_test.py` and `writeback_test.py` pin the input text and the Linear calls; `run_test.py` pins the tmux argv.
- Each pipeline task (deep-research, light-research, product-design, build, light-build) has exactly one `[agent-pm-progress:start]` line (`core/src/tests/compose_test.py` checks); without it write-back posts no start comment, and a build's `issues.build_cutoff` loses its `Build started` cutoff.
- Never name Linear in core prompts; `compose_test.py` fails on it.
- Identify Linear entities by id, never name; a role's `account` (an email) is the exception.
- A tmux `-t agent-pm-<role>-<ID>` whose session is gone prefix-matches another: target `'=agent-pm-<role>-<ID>'` (quoted: zsh expands a leading `=`). The code targets only with `=<name>`: the router reads `list-sessions`, `run.py` names via `new-session -s`, `attended.close` kills by exact target (`tui_claude.kill`).
- Tmux session names `<role>-<ID>-<8 hex>` (role `[a-z][a-z0-9-]*`) are reserved for agent-pm TUI runs: `attended.close` and prune kill any session so named.
- Keep `<work_dir>/logs`: router state, and the plists' hard-coded log dir (launchd can't start a job without it). Schedules (`orchestrator/*.plist`, installed as copies): README › Setup.
- Linear's lists can lag a just-made state change; re-read an issue's state before acting on it.
- A `[local_clones]` clone must not move: that breaks its worktrees. Run `git -C <new path> worktree repair`, then update the entry.

## Agent skills

### Domain docs

Single-context: root `CONTEXT.md` plus `docs/adr/`. See `docs/agents/domain.md`.
