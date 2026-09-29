# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Unattended agent pipeline driven by a Linear board: a router runs the runnable projects in `pipeline.toml` (Deep Research, Product Design, Engineering), each by a role doing a task, and promote hands issues between projects. README.md covers operations (schedule, Keychain, rescheduling); read it first.

## Commands

```bash
python3 -m unittest discover -s scripts/tests            # all tests (Python 3.11+); no network, Keychain or Claude
python3 -m unittest discover -s scripts/tests -k attempt # tests matching a name
python3 scripts/router.py --now --dry-run               # one tick: plan + usage probe, changes nothing
python3 scripts/router.py --plan --dry-run               # Linear side only
python3 scripts/promote.py --dry-run                     # what Handoff would do
python3 scripts/promote.py --now                         # promote now, skipping the 10-minute wait
```

A router tick without `--dry-run` claims an issue and starts claude; `--pick` claims too.

## Architecture

- **Router** (`scripts/router.py`, launchd `com.ophis.agent-pm.router`): hours → tmux lock (session `agent-pm`) → prune → Recover across runnable projects → plan → usage gate → resume or claim (priority, later stage first, oldest) → `start`/`resume` line → `launch.py`. Decisions go to `logs/router.log`.
- **Launcher** (`scripts/launch.py`): looks the project up in `pipeline.toml` (`runnable()` resolves its `role` and `task` from their `roles/` and `tasks/` pairs) and starts `claude -p` in tmux; output and the `end` line go to `logs/projects/<task>.log`, the `end` line also to `runs.log`.
- **Runner vs role and task**: `scripts/` decides what runs; `roles/<role>.md` is the role's charter (identity: responsibilities, standards, boundaries, memory; no task steps), `tasks/<task>.md` what the agent does (filling a skeleton from `templates/` where it has one), `roles/principles.md` the rules every role and task shares (precedence principles > charter > task steps). All three pass by absolute path in the prompt (not skills), plus the role's `memory` dir when set. Every task file must define a resume rule; resumed sessions re-read all three mid-flight. The registry: `roles/<role>.md` + `roles/<role>.toml` (`read_only`, `memory`), `tasks/<task>.md` + `tasks/<task>.toml` (`model`, `effort`, `add_dirs`, `repo_from_issue`, `allowed_tools`), and `pipeline.toml` with only `team`, `human_members` and `[projects."<id>"]` (`role` + `task` = runnable; `next`, `prefix`, `require_instructions`); `runnable()` rejects an unpaired file, a bad name, unknown keys and a `memory` overlapping the repo, a `read_only` path, `~/.claude` or `~/Library/LaunchAgents` (either direction); the launcher exits 2 (`config-error` in the project log) for a `memory` overlapping the issue's clone or worktrees.
- **Where a rule goes**: ask whether it still holds for another task of the same role. Every role → `roles/principles.md`; every task of one role → the charter `roles/<role>.md`; a format → `templates/`; only this task (claiming, the workflow call, failure handling, hand-off, resume rule) → `tasks/<task>.md`. One rule, one place. When `pipeline.toml` gives a role a new task, review all of that role's tasks together and lift what they share into the charter (what every role shares into `principles.md`, formats into `templates/`).
- **State**: `logs/runs.log` (start/resume/end only) plus Linear issue history are the only memory between ticks. Recover derives liveness from transcript mtimes and the current SID from runs.log lines newer than the issue's last move to In Progress.
- **Shared module** (`scripts/pipeline.py`): Linear access, config loading, paths. Every run's cwd is `run_dir(ID)` = `work/<ID>/`, and the router finds its session at `transcript(ID, sid)` = `~/.claude/projects/<escape(run_dir)>/<sid>.jsonl` (Claude keys transcripts by cwd), so moving the repo or `work/` orphans any resumable session. The LaunchAgent plists hold absolute paths; reinstall them after a move.
- **Engineering** (`tasks/engineering.md`, `scripts/eng.py`): for `repo_from_issue` tasks the launcher resolves the issue's `Repo:` line (Ok / Invalid / Transient) before starting; the agent has `autopilot:build` work in `work/<ID>/worktrees/<branch>` and runs git/`gh` itself. `eng.py status`/`comments` is a correctness aid, not a security boundary.
- **Handoff** (`scripts/promote.py`, its own LaunchAgent every 15 min, no LLM): Handoff issues → next project per `pipeline.toml`. The child issue's id is derived from the source, target project and handoff time, which is what makes reruns idempotent.
- **Linear access**: `pipeline.py` calls the GraphQL API directly (key from Keychain, account `frank.agent.w`); agents use the `linear` skill (`~/.claude/skills/linear`). No Linear MCP.

## Gotchas

- A `claude -p` run can't touch anything outside its cwd `work/<ID>/` unless it is added with `--add-dir`: it stops for a permission nobody can grant. Runs get `--setting-sources user --strict-mcp-config` (this file does not load in runs), `--add-dir` for `roles/`, `tasks/`, `templates/`, the task's `add_dirs` and the role's `memory`, and Edit deny rules (`//<absolute path>`) for `roles/`, `tasks/`, `templates/`, worktree `.git` files and the role's `read_only` (`{repo}` = the issue's clone and `work/<ID>/worktrees/`); any new path a task needs goes in its `add_dirs`. Even so, auto mode held a `python3 ../scripts/...` call for approval in a test run.
- `test_launch.py` asserts exact prompt strings and `claude` flags; update it with any prompt or flag change.
- `pipeline.toml` keys projects (and `next`) by Linear project id, so renaming a project needs no change; the team is still matched by name.
- Board rules the code relies on: the agent never moves issues to Backlog; anything needing the user goes to In Review, assigned to the first `human_members` user (Recover only touches In Progress issues assigned to the agent); moving an issue back to Todo resets its attempt cap (4).
- Design specs and autopilot plan docs are committed under `docs/specs/` (spec-review snapshots `*.r<N>.md` are ignored). `logs/` is gitignored and exists only locally. `logs/runs.log` is runner state; never clean `logs/`. launchd jobs fail to start if `logs/` is missing.
- promote's Handoff query nests `attachments` without a `first` limit and lists at most 100 issues; fine at current volume, but if Linear starts rejecting it as too complex (limit 10000), add `attachments(first: N)`.
- Linear's issue lists can lag a just-made state change, and its history may omit moves made right after creation; promote re-reads the issue's state before acting.
- launchd runs router and promote with `/opt/homebrew/bin/python3`; macOS `/usr/bin/python3` is 3.9 and lacks `tomllib`.
