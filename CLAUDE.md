# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Unattended agent pipeline driven by a Linear board: launchd-scheduled Python scripts pick Todo issues assigned to a role account (any project; the project is the product, the assignee is the stage: researcher → pm → engineer) and start one `claude -p` run per issue; each run is that role (`roles/<role>.md`) doing its default task (`tasks/<task>.md`). Design history: `docs/specs/`.

## Commands

```bash
python3 -m unittest discover -s scripts/tests            # all tests; no network, Keychain or Claude
python3 -m unittest discover -s scripts/tests -k attempt # tests whose name matches
python3 scripts/router.py --now --dry-run                # one tick: plan + usage probe, changes nothing
python3 scripts/promote.py --dry-run                     # what Handoff and prune would do
tmux attach -t agent-pm                                  # watch the live run
```

Python 3.11+ (`tomllib`); launchd uses `/opt/homebrew/bin/python3` because macOS `/usr/bin/python3` is 3.9. Without `--dry-run`, a router tick or `--pick` claims a real issue and starts claude, and promote mutates Linear.

## Architecture

- `scripts/router.py` (hourly 01:00–06:00): tmux lock (session `agent-pm`) → Recover (resume or requeue dead In Progress runs) → usage gate → resume or claim → `launch.py`. Queue and Recover cover the team's issues in a project assigned to a role account (matched by Linear user id); claim and requeue change only the state, so the assignee stays. Order: priority, later role, oldest.
- `scripts/launch.py` starts `claude -p` in tmux with cwd `work/<ID>/`. The role is the one whose `account` is `--assignee` (case-insensitive), running its default task (first in `tasks`); `--project` only fills the prompt's `Project:`. It checks the role's Keychain item (`security find-generic-password -s <key>`, no `-w`; missing → `config-error`, exit 2) and exports `LINEAR_KEYCHAIN_SERVICE=<key>`, so the run acts as the role's Linear account; it never reads a key. The prompt passes `roles/principles.md`, the charter and the task by absolute path, plus a tail line of ids (`Humans:`, `Project:`, `Team:`, `States:`) and, for `repo_from_issue` tasks, `Repo check:` from `eng.resolve`.
- `scripts/eng.py`: the Engineering repo step (the issue's `Repo:` line, else its project's `[project_repos]` entry; clone into `~/playground/<name>`, pick the `<ID>-<slug>` branch) and a `status`/`comments` CLI the Engineering run calls.
- `scripts/promote.py` (every 15 min, no LLM): Handoff issues in a project, assigned to a role account whose `[roles.<role>]` has `next` → a new issue in the same project, assigned to the next role's account; the child id is a hash of source, next role and handoff time, which makes reruns idempotent. Each tick ends with `prune.py` (force-deletes worktrees of issues finished ≥ 24 h ago).
- `scripts/pipeline.py`: shared Linear GraphQL client (the harness account's key, from the Keychain service `harness_key` in `pipeline.toml`), config loading and validation (`load_config`, `registry`, `runnable`, `role_ids`). Router, launcher and promote run `runnable`; router and promote also `role_ids`, which stops them when a role account is missing in Linear.
- Identity: router, promote and prune act as the harness account `frank.agent.w@gmail.com`; each run as its role's `account` (`roles/<role>.toml`). Only Keychain service names travel in env vars, argv, logs and prompts, never keys.
- Every move to In Review, by a run (`roles/principles.md`) or the harness (router's attempt cap, promote's bounce), also subscribes each `human_members` email (`issueSubscribe`) and keeps the assignee.
- State between ticks is only `logs/runs.log` (start/resume/end lines) plus Linear issue history. Resume finds a session at `~/.claude/projects/<escaped work/<ID>>/<sid>.jsonl`, so moving the repo or `work/` orphans resumable sessions.

## Roles, tasks, rules

- Every `roles/<role>` and `tasks/<task>` is an `.md` + `.toml` pair, and every role is runnable: its default task is the first in `tasks`. `pipeline.toml`'s `[roles.<role>]` holds `next` and `require_instructions`. Allowed keys are the `*_KEYS` sets in `pipeline.py`; an unpaired file or unknown key stops router, launcher and promote.
- Where a rule goes: holds for every role → `roles/principles.md`; every task of one role → the charter `roles/<role>.md`; a document format → `templates/`; this task only (claiming, failure, hand-off, resume) → `tasks/<task>.md`. One rule, one place. Precedence: principles > charter > task.
- Every task file defines a resume rule; a resumed session re-reads all three files.

## Gotchas

- Runs get `--setting-sources user --strict-mcp-config`, so this file and project settings don't load in them. A run reaches only `work/<ID>/` and its `--add-dir`s (`roles/`, `tasks/`, `templates/`, the task's `add_dirs`, the role's `memory`); give a task any new path through `add_dirs`, or the run stalls on a permission nobody can grant.
- `test_launch.py` asserts exact prompt strings and `claude` flags; update it with any prompt or flag change.
- `pipeline.toml` refers to the team and states by Linear id (names are labels; code and tasks act by id), and to roles by name.
- `logs/` (gitignored) is runner state: keep it; launchd can't start a job whose log dir is missing.
- Schedules are `scripts/*.plist`, installed as copies in `~/Library/LaunchAgents/` (reload with `launchctl bootout` + `bootstrap`); they hold absolute paths, and router's 01–06 hours check must match its plist.
- Linear's issue lists can lag a just-made state change; re-read the issue's state before acting on it.
