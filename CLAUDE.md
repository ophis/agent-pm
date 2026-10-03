# CLAUDE.md

Unattended agent pipeline on a Linear board. launchd runs `router.py`, which starts one `claude -p` run per Todo issue assigned to a role account (researcher → pm → engineer; the project is the product, the assignee the stage). A run is that role (`roles/<role>.md`) doing one task (`tasks/<task>.md`): the one the issue's `Tasks` label picks through `pipeline.toml`'s `[task_labels]`, else the role's default.

## Commands

```bash
python3 -m unittest discover -s scripts/tests            # all tests; no network, Keychain or Claude
python3 -m unittest discover -s scripts/tests -k attempt # tests whose name matches
python3 scripts/router.py --now --dry-run                # one tick: plan + usage probe, changes nothing
python3 scripts/promote.py --dry-run                     # what Handoff and prune would do
tmux ls                                                  # running sessions, agent-pm-<role>
tmux attach -t agent-pm-<role>                           # watch a role's live run
```

Python 3.11+ (`tomllib`); launchd uses `/opt/homebrew/bin/python3`, since macOS's is 3.9. Without `--dry-run`, router and promote change real issues and start real runs.

## Architecture

- `router.py` (every 30 min): per-role tmux locks → Recover dead In Progress runs → usage gate → resume or claim → `launch.py`, at most one launch per tick. It claims only ready Todo issues (every direct blocker Done, Canceled or Duplicate) and changes only their state, never the assignee. `task_for` resolves the task from the `Tasks` label's id; a bad label → comment, In Review, no run.
- `launch.py` starts the run in tmux `agent-pm-<role>`, cwd `work/<ID>/`, as the role's Linear account (`LINEAR_KEYCHAIN_SERVICE`). The prompt names principles, charter and task by path, plus a tail of ids. A `read_repo` run gets three exact `--allowedTools` rules, the only exceptions to `pipeline.check_allowed_tools`; their text (`PROBE`, `research.py prepare`) must match `tasks/deep-research.md` and `roles/researcher.md`.
- `sessions.py`: one `Run <sid> · …` comment per session. It uses `harness_key`, not the run's `LINEAR_KEYCHAIN_SERVICE`, so `isMe` is the harness account. Promote leaves these comments out of `## Comments`; runs skip them.
- `eng.py`: the Engineering repo step (`resolve`), the `status`/`comments` CLI, and repo helpers shared with `research.py` and `prune.py`.
- `research.py prepare`: a detached worktree of the target repo at `work/<ID>/src/<name>`; exit 2 = invalid target, 1 = other failure. It writes to the clone only through `gh repo clone`, `fetch` and `worktree add`/`remove`.
- `promote.py` (every 5 min): Handoff → a Todo issue for the next role, its id hashed from source, role and handoff time, so reruns are idempotent. Each tick ends with `prune.py`: it deletes worktrees of issues finished ≥ 24 h ago and archives pm and engineer ones (hence engineering's `issueUnarchive`).
- `pipeline.py`: the Linear client (harness account) and config validation.
- Identity: scripts act as the harness account `frank.agent.w@gmail.com`, runs as their role's `account`. Only Keychain service names travel in env, argv, logs and prompts, never keys.
- Every move to In Review, by a run or the harness, subscribes each `human_members` email and keeps the assignee.
- State between ticks is only `logs/runs.log` (resume and Recover take the task from its `task=`, never the labels) and Linear history; the router never reads session comments. Resume needs `~/.claude/projects/<escaped work/<ID>>/<sid>.jsonl`, so moving the repo or `work/` orphans sessions.

## Roles, tasks, rules

- Every `roles/<role>` and `tasks/<task>` is an `.md` + `.toml` pair; a role's default task is first in its `tasks`. Allowed keys are `pipeline.py`'s `*_KEYS`; an unpaired file or invalid config stops router, launcher and promote.
- Where a rule goes: every role → `roles/principles.md`; every task of one role → its charter; a document format → `templates/`; one task (claiming, failure, hand-off, resume) → `tasks/<task>.md`. One rule, one place. Precedence: principles > charter > task.
- Every task has a resume rule; rules shared by every resume live in principles.

## Gotchas

- Runs get `--setting-sources user --strict-mcp-config`, so this file and project settings never load in them. A run reaches only `work/<ID>/` and its `--add-dir`s; give a task a new path through `add_dirs`, or it stalls on a permission nobody can grant.
- `test_launch.py` asserts exact prompt strings and `claude` flags.
- Identify Linear entities by id, never name; a role's `account` (an email) is the exception.
- tmux targets use `=agent-pm-<role>` for an exact match.
- Keep `logs/` (gitignored): it is runner state, and launchd can't start a job whose log dir is missing.
- Schedules are `scripts/*.plist`, installed as copies in `~/Library/LaunchAgents/` (reload: `launchctl bootout` + `bootstrap`). The router plist's `--now` skips the 01–06 h check.
- Linear's lists can lag a just-made state change; re-read an issue's state before acting on it.
