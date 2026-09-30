# agent-pm

Runs Claude agents unattended from a Linear board. Each Linear project is a product; an issue's assignee, a role account (researcher, pm, engineer), is its stage. The agent works one issue at a time overnight and hands its output back to you for review.

| Role | Agent produces | Published to |
|---|---|---|
| researcher | A verified research report | `private_docs/Research/` |
| pm | A PRD | `private_docs/Product Design/` |
| engineer | A pull request that builds the PRD | The target repo |

## Using the board

- **New work:** create an issue in the product's project, assigned to the role account that should do it, in Todo; an issue not assigned to a role account (unassigned, or to a person) is never picked. Researcher and pm issues take the brief from the description; a direct engineer issue needs a `Repo: <owner>/<name>` line in its description unless its project has a `[project_repos]` entry.
- **Your turn:** the agent moves an issue to In Review and subscribes you when output is ready, it has questions, or it failed.
- **Approve:** move the issue to Handoff with a comment saying what to do next. For a PRD, the comment must include `Repo: <owner>/<name>` unless the project has a `[project_repos]` entry; a `Repo:` line always wins. After 10 minutes (an undo window), promote creates the next role's issue in the same project, assigned to that role, and marks this one Done.
- **Revise:** comment your feedback and move the issue back to Todo. The agent picks up where it left off. For an engineer issue, your PR comments and reviews count too; other authors' PR comments go to the build as review input, and only yours start a new build.
- **Finish:** Done and Canceled are yours to set. Worktrees of finished Engineering issues are deleted 24 hours later, along with any unpushed work.

An issue still unfinished after 4 attempts goes to In Review; a `human_members` user moving it back to Todo resets the count.

## Schedule

- **Router:** hourly 01:00–06:00. Resumes an interrupted run or claims the top Todo issue (priority, then later role, then oldest), one run at a time. Skips the tick while 5-hour usage is at 90% or more, or a weekly limit is full.
- **Promote:** at :05, :20, :35 and :50. Handles Handoff, then prunes finished worktrees.

## Setup

Requires macOS, `/opt/homebrew/bin/python3` (3.11+), `tmux`, `git`, `gh` (logged in as you), and the `claude` CLI with the `linear` skill (honouring `LINEAR_KEYCHAIN_SERVICE`) and the `autopilot` plugin. `~/playground/private_docs` must be a clone of `ophis/private_docs`.

```bash
security add-generic-password -a frank.agent.w -s linear-api-key -w   # harness account's Linear API key; the only item under pipeline.toml's harness_key
mkdir -p logs                                                         # launchd can't start a job without it
for job in router promote; do
  cp scripts/com.ophis.agent-pm.$job.plist ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ophis.agent-pm.$job.plist
done
```

To change a schedule, edit the plist in `scripts/` (for the router, also its hours check in `router.py`), copy it again, then `launchctl bootout gui/$(id -u)/com.ophis.agent-pm.<job>` and bootstrap it again. To stop a job, `bootout` it and delete its plist from `~/Library/LaunchAgents/`.

### Role accounts

Each role in `roles/*.toml` acts in Linear as its own `account`, with its API key in the Keychain under `key`:

1. Linear → Settings → Members: invite the `account` (a Gmail plus-alias of the harness account, e.g. `frank.agent.w+pm@gmail.com`).
2. Open the invite in a private window and choose **Continue with email**; Continue with Google signs in as the harness account.
3. Signed in as the role account, create a personal API key in its account settings.
4. `security add-generic-password -s <key> -a <account> -w` and paste the key at the prompt.

The launcher never reads the key: it checks the item exists and sets `LINEAR_KEYCHAIN_SERVICE=<key>` for the run's `linear` skill. A missing item stops that role's runs with a `config-error` line in `logs/projects/<task>.log`.

## Operating

```bash
python3 scripts/router.py --now --dry-run           # what the next tick would do; changes nothing
python3 scripts/router.py --now                     # run a tick now, outside the schedule
python3 scripts/router.py --now --issue TASK-12     # start a specific Todo issue
python3 scripts/router.py --pick --role researcher  # recover, then claim the role's top Todo issue
python3 scripts/promote.py --now                    # handle Handoff now, skipping the 10-minute wait
tmux attach -t agent-pm                             # watch the live run
cd work/TASK-12 && claude --resume <session-id>     # open a run's session (id from logs/runs.log)
```

| Log | Contents |
|---|---|
| `logs/router.log` | Each tick's decisions |
| `logs/projects/<task>.log` | Each run's output |
| `logs/promote.log` | Handoff and prune actions |
| `logs/runs.log` | Run start/resume/end; the router needs it to resume, so keep it |

Every run works in `work/<ID>/`. Moving the repo or `work/` breaks resuming in-progress runs and the installed plists.

## Configuration

- `pipeline.toml`: the Linear team and workflow states, both by id; `human_members`; `harness_key`, the Keychain service of the harness account's key; per role (`[roles.<role>]`) its `next` role and `require_instructions`; `[project_repos]`, each Linear project id → the `<owner>/<name>` repo of its Engineering issues that have no `Repo:` line.
- `roles/`: `principles.md` (rules for every run) and one charter per role, each with a `.toml` of settings: `tasks` (first is the default), `account`, `key`, `read_only`, `memory`.
- `tasks/`: the steps for each stage, each with a `.toml` (model, effort, extra dirs, title `prefix`, required on the default task of any role that is some role's `next`).
- `templates/`: the report and PRD skeletons.

## Cutover (TASK-62 PR-C)

Moving a running pipeline from stage projects to assignees:

1. Merge the PR without deploying.
2. Create a Linear project per product.
3. Stop the pipeline: no `agent-pm` tmux session, every `start` in `logs/runs.log` has an `end`, and don't run the router by hand.
4. Reassign every open issue (Todo, In Progress, In Review, Handoff) by its stage project: 1-Research → researcher, 2-Product Design → pm, 3-Engineering → engineer. An issue not assigned to a role account (unassigned, or to a person) is never picked, recovered or handed off.
5. `git pull --ff-only` in `~/playground/agent-pm`.
6. `python3 scripts/router.py --now --dry-run` logs a `plan:` line counting the role accounts' Todo issues (`plan: new (N in queue)`); after the next promote tick, `logs/promote.log` has no error.

## Development

```bash
python3 -m unittest discover -s scripts/tests   # no network, Keychain or Claude needed
```

Architecture notes for Claude are in `CLAUDE.md`.
