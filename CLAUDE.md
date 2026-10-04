# CLAUDE.md

Unattended agent pipeline on a Linear board. launchd runs `router.py`, which starts one `run.py` per Todo issue assigned to a role account (researcher → pm → engineer; the project is the product, the assignee the stage). A run is a core role doing one core task (`core/team/roles/<role>.md`, `core/team/tasks/<task>.md`): the one the issue's `Tasks` label picks through `orchestrator/config.toml`'s `[task_labels]`, else the role's default. `core/` knows nothing of Linear (`core/CLAUDE.md`); the orchestrator (`orchestrator/src/`, config `orchestrator/config.toml`) turns an issue into the run's input and the run's outcome into Linear changes.

## Commands

```bash
python3 -m unittest discover -s orchestrator/src/tests -p "*_test.py"      # orchestrator tests; no network, Keychain or Claude
python3 -m unittest discover -s orchestrator/src/tests -p "*_test.py" -k attempt   # tests whose name matches
python3 -m unittest discover -s core/src/tests -p "*_test.py"     # core tests
core/regen_skills.sh                                              # after editing core/team, core/output, core/config or core/src/repo.py; commit core/skills/
python3 orchestrator/src/router.py --now --dry-run                         # one tick: plan + usage probe, changes nothing
python3 orchestrator/src/promote.py --dry-run                              # what Handoff and prune would do
tmux ls                                                           # running sessions, agent-pm-<role>-<ID>
tmux attach -t '=agent-pm-<role>-<ID>'                            # watch one run
```

Python 3.11+ (`tomllib`); launchd uses `/opt/homebrew/bin/python3`, since macOS's is 3.9. Without `--dry-run`, router and promote change real issues and start real runs.

## Architecture

- `router.py` (every 30 min): live tmux sessions vs each role's `max_runs` → Recover dead In Progress runs (one with a live session is skipped) → usage gate → resume or claim → `run.py`, at most one run per tick, for a role below its `max_runs`. It claims only ready Todo issues (every direct blocker Done, Canceled or Duplicate) and changes only their state, never the assignee. `task_for` resolves the task from the `Tasks` label's id; a bad label → In Review with a comment, no run. `--brake` is deep-research's second-round gate: a fresh usage probe that exits 0 only while 5-hour usage < 80%, no weekly limit is full and the status isn't `rejected` (no `rate_limit_event` → 1).
- `run.py`, outer (in the tick): validate → read the issue → engineering: `target.check` (Invalid on a new run → `writeback.bounce`, no run) → `inputs.gather` + `render` → `work/<ID>/input.md` → tmux `agent-pm-<role>-<ID>`, cwd `work/<ID>/`. Any failure starts nothing.
- `run.py`, inner (in tmux): `drive.plan` + `drive.start` with the sinks (terminal, `progress.jsonl`, `outcome.json`, the project log, `writeback.sink`) → `end` lines in `logs/runs.log` and the project log → `writeback.finish`. No valid outcome → the issue stays In Progress and Recover resumes it. It passes `config.layers(root)` (the overlay) to `drive.plan`; `config.run_config` mirrors that for validation (`runnable`, `docs`).
- `issues.py` reads an issue; `target.py` resolves its repo (`Repo:` line, else `[project_repos]`), runs the engineering pre-check and names the branch; `inputs.py` builds the input text per task kind, reading linked docs through `gh api`.
- `writeback.py`: the `start` mark and the outcome → Linear comments, title, attachment and state, as the role account. Any other mark posts as `Progress (<name>): <text>`, once per text. Per-task differences are `config.TASKS` data. `work/<ID>/writeback.json` ledgers each step per session, so a resume repeats none; a failed step leaves the issue In Progress.
- `sessions.py`: one `Run <sid> · …` comment per session, posted in-process by `run.py` with `harness_key`, not the role's key, so `isMe` is the harness account. Promote leaves these comments out of `## Comments`; `issues.read_issue` drops them.
- `promote.py` (every 5 min): Handoff → a Todo issue for the next role, its id hashed from source, role and handoff time, so reruns are idempotent. Each tick ends with `prune.py`: it deletes the clones (`work/<ID>/src/*`, `work/<ID>/publish`) of issues finished ≥ 24 h ago and archives pm and engineer ones (hence engineering's `issueUnarchive`).
- `config.py`: paths, config validation, core config with the overlay, `TASKS`. `linear.py`: the Linear client, lookups, the writes `comment`, `subscribe`, `comment_and_move` and `move` (re-reads the state; moves only from the expected one), `HISTORY` / `last_move`, and non-Linear helpers: `parse_time`, `stamp`, `append` (a timestamped line), `log`, `one_line`. `linear` imports `config`, never the reverse. `config` puts `core/src` on `sys.path`, so no `orchestrator/src/` module may be named `clients`, `compose`, `drive`, `repo`, `report` or `tui`, `core/src` (first on `sys.path`) must gain no `config` or `linear` module, and a module importing a core module imports `config` first. `repo.SHORT` and `err_text` serve the orchestrator too.
- Identity: the orchestrator acts as the harness account `frank.agent.w@gmail.com`; write-back and bounce act as the role's `account` (`key`). Only Keychain service names travel in env, argv, logs and prompts, never keys. A run gets no Linear key, but that is no enforced boundary: it runs as the same macOS user and could read Keychain items.
- Every move to In Review, by write-back, bounce or the harness, subscribes each `human_members` email and keeps the assignee.
- The router's state between ticks is only `logs/runs.log` (resume and Recover take the task from its `task=`, never the labels) and Linear history; it never reads session comments. A resumed run also reads `work/<ID>/writeback.json` and needs `~/.claude/projects/<escaped work/<ID>>/<sid>.jsonl`, so moving the repo or `work/` orphans sessions.

## Roles, tasks, rules

- Roles, tasks, methods, principles and templates are core's: `core/team/` (text), `core/config/config.toml` (tier, effort, `read`/`write`, `commands`, `output`, `language`), `core/config/clients/claude.toml`. A role's default task is its `default_task`.
- `orchestrator/config.toml` adds what core must not know: `[roles.<role>]` (`account`, `key`, `next`, `require_instructions`, `max_runs`), `[task_labels]`, `[project_repos]`, and `[core]`, the overlay: core run keys for the orchestrator's runs, layered after the client config (`config.overlay()` fills `{{root}}`). Title prefixes and write-back differences per task are `config.TASKS`. An invalid or inconsistent config stops router, run and promote (`config.runnable`).
- Where a rule goes: every role → `core/team/principles.md`; every task of one role → `core/team/roles/<role>.md`; a document format → `core/team/templates/`; a method → `core/team/methods/`; one task (claiming, failure, hand-off, resume) → `core/team/tasks/<task>.md`; a Linear fact (comment text, state, title) → `writeback.py` / `config.TASKS`. One rule, one place. Precedence: principles > charter > task.
- Every task has a `## Resume` section; the shared resume text is `compose.RESUME`.

## Gotchas

- Runs get `--setting-sources user --strict-mcp-config` (`core/config/clients/claude.toml`), so this file and project settings never load in them. A run reaches only `work/<ID>/` and the config's `read`/`write` dirs; give a task a new path there, or it stalls on a permission nobody can grant.
- `inputs_test.py` and `writeback_test.py` pin the input text and the Linear calls; `run_test.py` pins the tmux argv.
- Each pipeline task (deep-research, light-research, product-design, engineering) has exactly one `[agent-pm-progress:start]` line (`core/src/tests/compose_test.py` checks). Without it write-back posts no start comment, and engineering's `issues.build_cutoff` loses its `Build started` cutoff.
- Never name Linear in core prompts; `compose_test.py` fails on it.
- Identify Linear entities by id, never name; a role's `account` (an email) is the exception.
- A tmux `-t agent-pm-<role>-<ID>` whose session is gone prefix-matches another (TASK-1 hits a live TASK-12); target `'=agent-pm-<role>-<ID>'` (quoted: zsh expands a leading `=`). The code targets no session: the router reads `list-sessions`, `run.py` names its session with `new-session -s`.
- Keep `logs/` (gitignored): it is runner state, and launchd can't start a job whose log dir is missing.
- Schedules are `orchestrator/*.plist`, installed as copies in `~/Library/LaunchAgents/` (reload: `launchctl bootout` + `bootstrap`). The router plist's `--now` skips the 01–06 h check.
- Linear's lists can lag a just-made state change; re-read an issue's state before acting on it.

## Agent skills

### Domain docs

Single-context: root `CONTEXT.md` plus `docs/adr/`. See `docs/agents/domain.md`.
