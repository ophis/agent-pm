# agent-pm

Runs Claude agents unattended from a Linear board. Each Linear project is a product; an issue's assignee, a role account (researcher, pm, engineer), is its stage. The agent works one issue per role at a time and hands its output back to you for review.

## Files

```
agent-pm/
├── CLAUDE.md              # architecture notes for Claude working on this repo
├── pipeline.toml          # Linear team and state ids, task label group and label ids, humans, role order, project → repo map, docs repo
├── roles/
│   ├── principles.md      # rules for every role and task
│   └── <role>.md + .toml  # researcher, pm, engineer: charter; account, key, tasks
├── tasks/
│   └── <task>.md + .toml  # deep-research, light-research, product-design, engineering: steps; model, effort, dirs
├── templates/             # research-report.md, prd.md
├── scripts/
│   ├── router.py          # picks and starts the next run
│   ├── launch.py          # starts one claude run
│   ├── sessions.py        # records each run's session on its issue
│   ├── promote.py         # Handoff to the next role
│   ├── prune.py           # deletes finished issues' worktrees, archives pm and engineer ones
│   ├── eng.py             # engineering repo resolution and CLI
│   ├── research.py        # read-only checkout of a research issue's target repo
│   ├── pipeline.py        # shared Linear client and config
│   ├── *.plist            # launchd schedules for router and promote
│   └── tests/
├── work/<ID>/             # a run's working dir, its worktrees/ and a research run's src/ checkout (gitignored)
└── logs/                  # runner state and run output (gitignored)
```

## Roles

A role is who the agent is: a charter (`roles/<role>.md`: responsibilities, standards, boundaries, memory) and its own Linear account. An issue assigned to that account runs the task its `Tasks` label picks (see Task labels), else the role's default task. `roles/principles.md` holds the rules every role follows: act by Linear id, publish documents to the docs repo (`[docs]`) in Chinese, ask the user when an issue is too vague.

| Role | Tasks (first is default) | Does | Next |
|---|---|---|---|
| researcher | `deep-research`, `light-research` | Judges the issue web, local (needs one repo's code, read from a read-only checkout) or mixed (both), and answers its questions with a report: every finding with its confidence and sources (a URL; for code, a GitHub permalink at the commit the report names), claims you list as known re-checked, gaps listed | pm |
| pm | `product-design` | Turns a brief or research report into a PRD, adding no scope you didn't ask for and marking its own inferences as assumptions | engineer |
| engineer | `engineering` | Builds a PRD into a pull request on the target repo, following that repo's conventions; never merges, force-pushes or touches the default branch | — |

## Tasks

A task is the steps a run follows for one issue (`tasks/<task>.md`), including how to resume after an interruption.

- **`deep-research`:** for a web issue, runs the built-in `/deep-research` Workflow once over all the issue's questions; for a local one, writes and runs one workflow of its own (ultracode, ≤ 100 agents, verification inside) over the checkout; for a mixed one, at most that workflow plus one `/deep-research` run for the web part, skipping the second round when 5-hour usage is ≥ 80%. Writes the report from `templates/research-report.md` to `Research/` in the docs repo, attaches its link and moves the issue to In Review.
- **`light-research`:** for issues labeled `Light Research`. Sends 3–6 angles in one round, one agent each (web angles for a web issue, checkout angles for a local one, both for a mixed one), each checking its own sources, with no separate verification. Publishes a shorter report marked 轻量调研 like `deep-research`, aiming at 10 minutes or less. To upgrade, remove the label, comment the claims to verify and move the issue back to Todo: the next run is `deep-research`, which rewrites the same report.
- **`product-design`:** reads the brief and source reports; researches and grills itself with a subagent when needed; writes the PRD from `templates/prd.md` to `Product Design/` in the docs repo, has a fresh subagent review it and fixes what holds up; publishes it, retitles the issue `PRD: <product name>`, and asks you to approve it with a Handoff, naming the project's mapped repo if there is one.
- **`engineering`:** runs `autopilot:build` on the PRD in a worktree on an `<ID>-<slug>` branch of the target repo, pushes the branch, posts the build's spec and plan on the issue, opens the PR and links it. A failed build still pushes the branch and reports what failed.

## Harness

The Python scripts in `scripts/` that turn the board into runs. launchd runs the router and promote; they act in Linear as the harness account `frank.agent.w@gmail.com`, each run as its role's account.

| Script | Runs | Does |
|---|---|---|
| `router.py` | Every 30 minutes, all day | Decides what runs next. Recovers dead In Progress runs (resumes them or returns them to Todo), then starts at most one run per tick for a role with none going: resumes an interrupted run or claims the top ready Todo issue (priority, then later role, then oldest; one with an unfinished blocker isn't ready). Skips the tick when every role has a run going, while 5-hour usage is at 90% or more, or when a weekly limit is full. |
| `launch.py` | Called by the router | Starts one `claude -p` run in tmux session `agent-pm-<role>` with cwd `work/<ID>/`. It picks the role from the issue's assignee, runs the task named by `--task` (one of the role's tasks), checks the `[docs]` clone exists, points the run's `linear` skill at the role's Keychain key, and names the principles, charter and task files and the docs repo in the prompt. It limits the run to its own directories. For `engineering`, it resolves and clones the target repo first. For a `read_repo` task (both research tasks), it names the project's mapped repo in the prompt, lets the run call `research.py prepare` and the usage probe without approval, and bars it from editing the checkout, `scripts/` and the mapped repo's clone. |
| `sessions.py` | Each run's tmux session, before and after `claude` | Writes the session's `Run <sid>` comment on the issue (see Session records). |
| `promote.py` | Every 5 minutes | Hands off: after a 10-minute undo window, an issue in Handoff becomes a Todo issue for the next role in the same project, carrying the source's output links and your comments, and the source goes to Done. Each tick ends with `prune.py`. |
| `prune.py` | End of each promote tick | Deletes the worktrees (`worktrees/` and `src/`) of issues that have been Done or Canceled for 24 hours, along with any unpushed work, and archives the pm and engineer ones. |
| `eng.py` | Launcher and engineer runs | Resolves an Engineering issue's target repo and branch; its `status` and `comments` commands feed the engineer run. |
| `research.py` | Local and mixed research runs | `prepare` checks out the issue's target repo (cloned into `~/playground/<name>` if missing) at the latest commit of its default branch, detached, into `work/<ID>/src/<name>`; a resumed run reuses that checkout. The clone's files and branches stay as they are. |
| `pipeline.py` | Shared | Linear API client, and loading and validating the config. |

## Using the board

- **New work:** create an issue in the product's project, assigned to the role account that should do it, in Todo; an issue not assigned to a role account (unassigned, or to a person) is never picked. Researcher and pm issues take the brief from the description; a research issue about a repo other than its project's `[project_repos]` entry needs a `Repo: <owner>/<name>` line in its description, and so does a direct engineer issue unless its project has a `[project_repos]` entry.
- **Order work:** in Linear, add "Y blocks X": X is claimed only once every direct blocker is Done, Canceled or Duplicate (archived counts as done; an unreadable blocker as not done). The router skips a blocked X and logs `blocked: X by Y` to `logs/router.log`.
- **Your turn:** the agent moves an issue to In Review and subscribes you when output is ready, it has questions, or it failed.
- **Approve:** move the issue to Handoff, with a comment saying what to do next (optional for a PRD). A PRD's target repo is the project's `[project_repos]` entry, which the PM's hand-off comment names; without one, comment `Repo: <owner>/<name>`. A `Repo:` line in your comment always wins. After 10 minutes (an undo window), promote creates the next role's issue in the same project, assigned to that role, and marks this one Done.
- **Revise:** comment your feedback and move the issue back to Todo. The agent picks up where it left off. For an engineer issue, your PR comments and reviews count too; other authors' PR comments go to the build as review input, and only yours start a new build.
- **Finish:** Done and Canceled are yours to set. Worktrees of finished issues (an engineer's repo worktree, a researcher's or pm's docs worktree, a researcher's `src/` checkout) are deleted 24 hours later, along with any unpushed work; pm and engineer issues are archived then too, so restore one in Linear before reworking it.

An issue still unfinished after 4 attempts goes to In Review; a `human_members` user moving it back to Todo resets the count.

### Task labels

A role can have several tasks. The issue's label in the Linear `Tasks` label group (`task_label_group`) picks one by its id, through `pipeline.toml`'s `[task_labels]` (`<task> = "<label id>"`), so renaming a label keeps working; with no such label the issue runs the role's default task. The task must be one of the assignee role's. If the label isn't in `[task_labels]`, its task isn't one of the assignee role's, or an issue has several task labels, no run starts and no attempt counts: the router comments why, subscribes you and moves the issue to In Review. Fix the label, then move the issue back to Todo.

The task is recorded in `logs/runs.log`, so a resumed run keeps it whatever the labels say (none recorded: the role's default). An interrupted run whose task is no longer one of the role's goes to In Review with a comment.

To add a task to a role: create `tasks/<task>.md` and `.toml`, add it to `roles/<role>.toml`'s `tasks`, create the label in the `Tasks` group and add `<task> = "<label id>"` to `[task_labels]`.

## Setup

Requires macOS, `/opt/homebrew/bin/python3` (3.11+), `tmux`, `git`, `gh` (logged in as you), and the `claude` CLI with the `linear` skill (honouring `LINEAR_KEYCHAIN_SERVICE`) and the `autopilot` plugin. `[docs]`'s `clone` must be a clone of its `repo`; agents publish through per-run worktrees and leave its files alone, so `git pull` there to see their documents locally. `task_label_group` in `pipeline.toml` is the id of the Linear `Tasks` label group (from the Linear API: `issueLabels { nodes { id name isGroup } }`); the router stops if it isn't a label group or a `[task_labels]` id isn't one of its labels.

```bash
security add-generic-password -a frank.agent.w -s linear-api-key -w   # harness account's Linear API key; the only item under pipeline.toml's harness_key
mkdir -p logs                                                         # launchd can't start a job without it
for job in router promote; do
  cp scripts/com.ophis.agent-pm.$job.plist ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ophis.agent-pm.$job.plist
done
```

To change a schedule, edit the plist in `scripts/` (the router plist passes `--now`, which skips `router.py`'s 01:00–06:59 hours check; drop it to run only at night), copy it again, then `launchctl bootout gui/$(id -u)/com.ophis.agent-pm.<job>` and bootstrap it again. To stop a job, `bootout` it and delete its plist from `~/Library/LaunchAgents/`.

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
python3 scripts/router.py --now --issue TASK-12     # start a specific Todo issue, unless it is blocked
python3 scripts/router.py --claim --dry-run         # preview the next claim and the blocked lines; changes nothing
python3 scripts/router.py --pick --role researcher  # recover, then claim the role's top Todo issue that runs its default task
python3 scripts/promote.py --now                    # handle Handoff now, skipping the 10-minute wait
tmux ls                                             # running sessions, agent-pm-<role>
tmux attach -t agent-pm-<role>                      # watch a role's live run
```

To open a run's session, copy the command from the code block of its issue's `Run <sid>` comment (see Session records).

| Log | Contents |
|---|---|
| `logs/router.log` | Each tick's decisions, including each claim's task |
| `logs/projects/<task>.log` | Each run's output |
| `logs/promote.log` | Handoff and prune actions |
| `logs/runs.log` | Run start/resume/end; the router needs it to resume, so keep it |

Every run works in `work/<ID>/`. Moving the repo or `work/` breaks resuming in-progress runs and the installed plists.

### Session records

Each session the launcher starts or resumes gets one `Run <sid>` comment on its issue, written and edited by the harness account. Its first line is `Run <sid> · running · <start>` just before `claude` starts, then `Run <sid> · done · <start> → <end> · exit 0` (`interrupted` for any other exit code) when it ends; below it, a code block holds only the command that reopens the session as its role, `cd <work/ID> && LINEAR_KEYCHAIN_SERVICE=<key> claude --resume <sid>`. The router's resume of an interrupted session edits the same comment; a new claim (e.g. after Revise) adds one.

- A session comment is one by the harness account (by email) whose first line starts `Run <sid> · `. Promote leaves session comments out of the next issue's `## Comments`, runs skip them, and the router never reads them: it still resumes from `logs/runs.log`.
- Earlier sessions have a `Run <sid>` attachment instead, or nothing. The attachments stay, and promote still leaves them out of `## Source`.
- Before opening a session by hand, move its issue out of In Progress, or the router may resume the same session once it has been idle 30 minutes.
- A comment stuck at `running` with no `agent-pm-<role>` tmux session was killed before `claude` exited; `tmux ls` is the truth.
- A write is one attempt of at most 10 seconds; a failure only adds a `registry-error` line to `logs/projects/<task>.log` and never affects the run.

## Configuration

- `pipeline.toml`: the Linear team and workflow states, both by id; `task_label_group`, the id of the Linear `Tasks` label group; `[task_labels]`, each task → the id of its label in that group; `human_members`; `harness_key`, the Keychain service of the harness account's key; per role (`[roles.<role>]`) its `next` role and `require_instructions`; `[project_repos]`, each Linear project id → the `<owner>/<name>` repo of its Engineering and local or mixed research issues that have no `Repo:` line; `[docs]`, the docs repo: `repo` (`<owner>/<name>`), `clone` (local clone path, `~` allowed, must exist when a run starts) and `branch`, e.g. `ophis/private_docs`, `~/playground/private_docs`, `main`.
- `roles/`: `principles.md` (rules for every run) and one charter per role, each with a `.toml` of settings: `tasks` (first is the default), `account`, `key`, `read_only` (an entry `{docs_clone}` is the `[docs]` clone), `memory`.
- `tasks/`: the steps for each stage, each with a `.toml` (model, effort, extra dirs (`{docs_clone}` is the `[docs]` clone), `read_repo` (runs may check out the issue's target repo read-only through `research.py`), title `prefix`, required on the default task of any role that is some role's `next`).
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
