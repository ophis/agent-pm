# CLAUDE.md

Unattended agent pipeline on a Linear board. launchd runs `router.py`, which starts one `run.py` per Todo issue assigned to a role account (researcher → pm → engineer; the project is the product, the assignee the stage). An agent run is a core role doing one core task (`core/team/roles/<role>.md`, `core/team/tasks/<task>.md`): the one the issue's `Tasks` label picks through `orchestrator/config.toml`'s `[task_labels]`, else the role's default. `core/` knows nothing of Linear (`core/CLAUDE.md`); the orchestrator (`orchestrator/src/`, config `orchestrator/config.toml`) turns an issue into the run's input and the run's outcome into Linear changes.

## Commands

```bash
python3 -m unittest discover -s orchestrator/src/tests -p "*_test.py"      # orchestrator tests; no network, Keychain or Claude
python3 -m unittest discover -s orchestrator/src/tests -p "*_test.py" -k attempt   # tests whose name matches
python3 -m unittest discover -s core/src/tests -p "*_test.py"     # core tests
python3 -m unittest discover -s .claude/skills/tui-workers/scripts -p "*_test.py"   # tui-workers skill tests
core/regen_skills.sh                                              # after editing core/team, core/output, core/config or core/src/repo.py; commit core/skills/
python3 orchestrator/src/router.py --now --dry-run                         # one tick: plan + usage probe, changes nothing
python3 orchestrator/src/run.py --issue TASK-12 --tui                      # run one issue attended (README › Attended runs)
python3 orchestrator/src/promote.py --dry-run                              # what Handoff and prune would do
tmux ls                                                           # running sessions, agent-pm-<role>-<ID>
tmux attach -t '=agent-pm-<role>-<ID>'                            # watch one agent run
```

Python 3.11+ (`tomllib`); launchd uses `/opt/homebrew/bin/python3`, since macOS's is 3.9. Without `--dry-run`, router and promote change real issues and start real agent runs.

## Architecture

- `router.py` (every 30 min): live tmux sessions vs each role's `max_runs` → Recover dead In Progress agent runs (one with a live session is skipped) → usage gate → resume or claim → `run.py`, at most one agent run per tick, for a role below its `max_runs`. It claims only ready Todo issues (every direct blocker Done, Canceled or Duplicate) and changes only their state, never the assignee. `task_for` resolves the task from the `Tasks` label's id; a bad label → In Review with a comment, no agent run. `--tui` (by hand) launches the tick's agent run with `run.py --runner tui`. `--brake` is deep-research's second-round gate: a fresh usage probe that exits 0 only while 5-hour usage < 80%, no weekly limit is full and the status isn't `rejected` (no `rate_limit_event` → 1).
- `run.py`, outer (in the tick): validate → read the issue → engineering: `target.check` (Invalid on a new agent run → `writeback.bounce`, no agent run) → `inputs.gather` + `render` → `work/<ID>/input.md` → tmux `agent-pm-<role>-<ID>`, cwd `work/<ID>/`. Any failure starts nothing. `--runner tui` (`--split`, `--beside`): `attended.layout` places the TUI pane first (`tui.anchor`: `--beside`'s pane, else the caller's tmux session when a terminal shows it, else `$ITERM_SESSION_ID`'s pane; none → `attended.Bad`, exit 2). The driver session always starts detached, on the caller's tmux server, with `-e ITERM_SESSION_ID=…` (tmux's global copy may be stale); the inner's `tui.show` splits the anchor in iTerm2, else with `tmux split-window`.
- `run.py --issue ID --tui` (attended entry, by hand): refuses a live agent run (`router.live_sessions`) → `router.Board.take` (no hours, `max_runs` or usage gate) → `router.start_line` into `runs.log` → outer with `--runner tui`.
- `attended.py`: `layout`, and the harness-only record `logs/tui/<ID>` of an issue's TUI sessions, read only through it: the inner `close`s the recorded ones before every agent run (so a resume never runs beside an old `claude`) and `record`s a tui run's; `close` returns `Closed` results that the inner and prune word. A driver session holds the lock and a `max_runs` slot only while its driver runs (`tmux kill-session` → SIGHUP → exit 129, TUI session killed, Recover resumes); a TUI session (`drive.TUI_SESSION`) holds neither.
- `run.py`, inner (in tmux): `drive.plan` + `drive.start` with the sinks (terminal, `progress.jsonl`, `outcome.json`, the project log, `writeback.sink`) → `end` lines in `logs/runs.log` and the project log → `writeback.finish`. No valid outcome → the issue stays In Progress and Recover resumes it. It passes `config.layers(root)` (the overlay) to `drive.plan`; `config.run_config` mirrors that for validation (`runnable`, `docs`).
- `issues.py` reads an issue; `target.py` resolves its repo (`Repo:` line, else `[project_repos]`), runs the engineering pre-check, names the branch and, via `with_clone`, sets the path the input's `Repo:` names (the issue's existing checkout decides, else `[local_clones]`); `inputs.py` builds the input text per task kind, reading linked docs through `gh api`.
- `writeback.py`: the `start` mark and the outcome → Linear comments, title, attachment and state, as the role account. Any other mark posts as `Progress (<name>): <text>`, once per text. Per-task differences are `config.TASKS` data. `work/<ID>/writeback.json` ledgers each step per session, so a resume repeats none; a failed step leaves the issue In Progress.
- `sessions.py`: one `Run <sid> · …` comment per session, posted in-process by `run.py` with `harness_key`, not the role's key, so `isMe` is the harness account. Promote leaves these comments out of `## Comments`; `issues.read_issue` drops them.
- `promote.py` (every 5 min): Handoff → a Todo issue for the next role, its id hashed from source, role and handoff time, so reruns are idempotent. Each tick ends with `prune.py`: for issues finished ≥ 24 h ago (a record alone makes one a candidate) it `attended.close`s the TUI sessions, then cleans up as README › Using the board › Finish says (no git on a worktree; refs only in its local clone), and archives pm and engineer ones (hence engineering's `issueUnarchive`). `promote.main` sets `config.PATH` first: launchd's has no tmux.
- `config.py`: paths, config validation, core config with the overlay, `TASKS`. `linear.py`: the Linear client, lookups, the writes `comment`, `subscribe`, `comment_and_move` and `move` (re-reads the state; moves only from the expected one), `HISTORY` / `last_move`, and non-Linear helpers: `parse_time`, `stamp`, `append` (a timestamped line), `log`, `one_line`. `linear` imports `config`, never the reverse. `config` puts `core/src` on `sys.path`, so no `orchestrator/src/` module may be named `clients`, `compose`, `drive`, `repo`, `report` or `tui`, `core/src` (first on `sys.path`) must gain no `config` or `linear` module, and a module importing a core module imports `config` first. `repo.SHORT` and `err_text` serve the orchestrator too.
- Identity: the orchestrator acts as the harness account `frank.agent.w@gmail.com`; write-back and bounce act as the role's `account` (`key`). Only Keychain service names travel in env, argv, logs and prompts, never keys. An agent run gets no Linear key, but that is no enforced boundary: it runs as the same macOS user and could read Keychain items.
- Every move to In Review, by write-back, bounce or the harness, subscribes each `human_members` email and keeps the assignee.
- The router's state between ticks is only `logs/runs.log` (resume and Recover take the task from its `task=`, never the labels) and Linear history; it never reads session comments. A resumed agent run also reads `work/<ID>/writeback.json` and needs `~/.claude/projects/<escaped work/<ID>>/<sid>.jsonl`, so moving the repo or `work/` orphans sessions.

## Roles, tasks, rules

- Roles, tasks, methods, principles and templates are core's: `core/team/` (text), `core/config/config.toml` (tier, effort, `read`/`write`, `commands`, `output`, `language`; `[clients.claude]`). A role's default task is its `default_task`.
- `orchestrator/config.toml` adds what core must not know: `[roles.<role>]` (`account`, `key`, `next`, `require_instructions`, `max_runs`), `[task_labels]`, `[project_repos]`, `[local_clones]` (repo → local clone path; the input's `Repo:` names it, `config.runnable` checks it), and `[core]`, the overlay: core run keys for the orchestrator's agent runs, layered after `core/config/config.toml`'s `[clients.claude]` (`config.overlay()` fills `{{root}}`). Title prefixes and write-back differences per task are `config.TASKS`. An invalid or inconsistent config stops router, run and promote (`config.runnable`).
- Where a rule goes: every role → `core/team/principles.md`; every task of one role → `core/team/roles/<role>.md`; a document format → `core/team/templates/`; a method → `core/team/methods/`; one task (claiming, failure, hand-off, resume) → `core/team/tasks/<task>.md`; a Linear fact (comment text, state, title) → `writeback.py` / `config.TASKS`. One rule, one place. Precedence: principles > charter > task.
- Every task has a `## Resume` section; the shared resume text is `compose.RESUME`.

## Gotchas

- Agent runs get `--setting-sources user --strict-mcp-config` (`core/config/config.toml`'s `[clients.claude]`), so this file and project settings never load in them. An agent run reaches only `work/<ID>/` and the config's `read`/`write` dirs; give a task a new path there, or it stalls on a permission nobody can grant.
- `inputs_test.py` and `writeback_test.py` pin the input text and the Linear calls; `run_test.py` pins the tmux argv.
- Each pipeline task (deep-research, light-research, product-design, engineering) has exactly one `[agent-pm-progress:start]` line (`core/src/tests/compose_test.py` checks). Without it write-back posts no start comment, and engineering's `issues.build_cutoff` loses its `Build started` cutoff.
- Never name Linear in core prompts; `compose_test.py` fails on it.
- Identify Linear entities by id, never name; a role's `account` (an email) is the exception.
- A tmux `-t agent-pm-<role>-<ID>` whose session is gone prefix-matches another (TASK-1 hits a live TASK-12); target `'=agent-pm-<role>-<ID>'` (quoted: zsh expands a leading `=`). The code targets only with `=<name>`: the router reads `list-sessions`, `run.py` names its session with `new-session -s`, `attended.close` checks and kills recorded TUI sessions by exact target (`tui.status`/`tui.kill`).
- Keep `logs/` (gitignored): it is router state, and launchd can't start a job whose log dir is missing.
- Schedules are `orchestrator/*.plist`, installed as copies in `~/Library/LaunchAgents/` (reload: `launchctl bootout` + `bootstrap`). The router plist's `--now` skips the 01–06 h check.
- Linear's lists can lag a just-made state change; re-read an issue's state before acting on it.
- A `[local_clones]` clone must not move: that breaks the worktrees already opened from it. Run `git -C <new path> worktree repair`, then update the entry.

## Agent skills

### Domain docs

Single-context: root `CONTEXT.md` plus `docs/adr/`. See `docs/agents/domain.md`.
