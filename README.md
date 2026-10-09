# agent-pm

[![test](https://github.com/ophis/agent-pm/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/ophis/agent-pm/actions/workflows/test.yml?query=branch%3Amain)

Runs Claude agents unattended from a Linear board. Each Linear project is a product; an issue's assignee, a role account, is its stage. Each role works up to `max_runs` issues at once, handing its output back for your review; once you approve, the next role takes it. `core/` runs a role, which picks its task unless given one, and is a Claude Code plugin you can use alone (Install); `orchestrator/` runs it from the board (Setup). Developers: `CLAUDE.md`.

```mermaid
flowchart LR
    R["researcher<br>deep-research, light-research"] -->|"report → Approve → promote.py"| P["pm<br>product-design"]
    P -->|"PRD → Approve → promote.py"| E["engineer<br>build, light-build"] -->|pull request| Y[you]
    T[router.py] -.->|claims Todo issues| R & P & E
```

## Install

`/plugin marketplace add ophis/agent-pm`, then `/plugin install agent-pm@agent-pm` (a private repo: your git credentials). Requires the `claude` CLI, Python 3.11+ (stdlib only), tmux 3.3+ (the tmux skill, tui runs), `git` and `gh` logged in (checkouts, the `github` and `pull-request` destinations), the `autopilot` plugin (plus `superpowers` for `build`), GitHub and the web; `osascript`, `pgrep` and a running iTerm2 only for the default iTerm2 split. Nothing checks them: a missing one fails the agent run. Agent runs load your user settings (`~/.claude/CLAUDE.md`, skills, permissions, plugins but `agent-pm`) and no MCP servers; a trusted cwd adds its project settings, `CLAUDE.md`, skills and `.mcp.json` (`core/config.toml`'s `cwd` and `trusted_dirs`).

- `/agent-pm:tmux`: start and direct other Claude Code sessions (workers) in iTerm2 or tmux panes, or run a core role in a pane or headless: `core/skills/tmux/SKILL.md`.
- `/agent-pm:manage`: your guidelines for the commander session, the one you talk to, e.g. `ctmux`'s; run it there. Only you can invoke it, so a worker starting its own workers never gets it: `core/skills/manage/SKILL.md`.
- `ctmux <session>` in iTerm2: a manager `claude --permission-mode auto` in tmux session `<session>` (made in the current dir if missing), attached with `tmux -CC`, so worker panes are native iTerm2 splits in the same window. Install: add `alias ctmux=~/.claude/plugins/marketplaces/agent-pm/core/skills/tmux/scripts/ctmux` to `~/.zshrc` (or `~/.bashrc`); the marketplace clone, not the versioned cache, keeps the path across updates.
- `/agent-pm:act-as <role>[:<task>] <input>`: run a core role in this conversation, on that task or one it picks: `core/skills/act-as/SKILL.md`.

## Setup

Requires macOS, `/opt/homebrew/bin/python3`, Install's requirements and `gh` logged in as you. The orchestrator runs this checkout's scripts, not the plugin.

1. Create `~/.agent-pm/orchestrator.local.toml` and `~/.agent-pm/core.local.toml` as each `config.toml`'s header says, core's with researcher's and pm's `[output]` (its Destination comment). `<work_dir>` below is the `work_dir` key.
2. Find `task_label_group` with the Linear API's `issueLabels { nodes { id name isGroup } }`; the router stops if it isn't a label group or a `[task_labels]` id isn't one of its labels.
3. Agent runs need no `linear` skill and get no Linear key: router and promote act in Linear as the harness account `frank.agent.w@gmail.com`, each role's write-back as its `[roles.<role>]` `account`, its API key in the Keychain under `key`. Per role:
   1. Linear → Settings → Members: invite the `account` (a Gmail plus-alias of the harness account, e.g. `frank.agent.w+pm@gmail.com`).
   2. Open the invite in a private window, choosing **Continue with email** (Google signs in as the harness account).
   3. As the role account, create a personal API key in its settings.
   4. `security add-generic-password -s <key> -a <account> -w`, pasting the key at the prompt. `router.py` checks the item exists; a missing one stops that role's agent runs with a `config-error` event in `<work_dir>/logs/orchestrator.jsonl`.
4. The harness account's key, the logs dir and the schedules:
   ```bash
   security add-generic-password -a frank.agent.w -s linear-api-key -w   # the only item under orchestrator.local.toml's harness_key
   mkdir -p ~/.agent-pm/logs                                             # the plists' log dir; launchd can't start a job without it
   for job in router promote; do cp orchestrator/com.ophis.agent-pm.$job.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ophis.agent-pm.$job.plist; done
   ```

The plists log to `/Users/francis/.agent-pm/logs/` (launchd doesn't expand `~`): with another `work_dir`, edit their log paths. The router plist passes `--now`, which skips `router.py`'s 01:00–06:59 hours check: drop it to run only at night. To change a schedule, edit the plist in `orchestrator/`, copy it again, `launchctl bootout gui/$(id -u)/com.ophis.agent-pm.<job>` and bootstrap it again; to stop a job, `bootout` it and delete its plist from `~/Library/LaunchAgents/`.

## Using the board

```mermaid
flowchart LR
    T[Todo] -->|router claims| P[In Progress]
    T -->|"bad Tasks label, or 4 attempts"| R[In Review]
    P -->|"output, questions, failure or 4 attempts; you subscribed"| R
    P -->|"failed new research run, or Recover"| T
    R -->|Approve| H[Handoff]
    R -->|Revise| T
    R -->|you| C[Canceled]
    P -->|"failed repo check of a handed-off build; its source → In Review"| C
    H -->|"promote, after the 10-min undo window: the next role's Todo issue"| D[Done]
    D & C -->|"Finish, 24 h later"| X[cleaned up]
```

- **New work:** a Todo issue in the product's project, assigned to the role account that should do it; others (unassigned, or a person) are never picked. Researcher and pm issues take the brief from the description; a direct engineer issue's text is its requirement. A research or pm issue about a repo other than its project's `[project_repos]` entry, or a direct engineer issue whose project has none, needs a `Repo: <owner>/<name>` line.
- **Order work:** the router claims by priority, then later role, then oldest. "Y blocks X": X is claimed only once every direct blocker is Done, Canceled or Duplicate (archived counts as done, an unreadable blocker as not done); the router logs a `blocked` event naming Y.
- **Approve:** move to Handoff, commenting what's next (optional for a PRD). A PRD's target repo is the project's `[project_repos]` entry, which the PM's hand-off comment names; without one, comment `Repo: <owner>/<name>`; a `Repo:` line in your comment always wins. After a 10-minute undo window (pull it back out of Handoff), promote creates the next role's issue in the same project and marks this one Done; it carries the source's output links and your comments.
- **Revise:** comment, move back to Todo; the agent picks up where it left off. On an engineer issue your PR comments and reviews count too; other authors' reach the build as review input, but only yours start a new build. After 4 attempts an unfinished issue goes to In Review; a `human_members` user moving it back to Todo resets the count.
- **Finish:** Done and Canceled are yours to set. 24 hours later, `src/` (clones and worktrees), `publish/` (the docs clone) and `tmp/` in `<work_dir>/work/<ID>/` are deleted; the rest stays. In each local clone those worktrees came from, prune drops their records and deletes the issue's `<ID>-*` branches that origin holds, keeping and reporting the others. Lost: uncommitted changes, commits never pushed from a detached HEAD or a submodule, and, as `git worktree prune` covers the whole clone, the record of any of your worktrees whose folder is missing then, unless you `git worktree lock` it. pm and engineer issues are archived then too: restore one in Linear before reworking it.

### Task labels

The issue's label in the `Tasks` label group names one of the assignee role's tasks by its id, through `[task_labels]`, so renaming a label keeps working: the agent run does exactly that task. No such label → the agent run picks one from its role's task index (`core/team/roles/<role>.md` › Tasks: what each task does, when to use it, how heavy; unsure → the default it names), and its start comment names the pick and why. A label not in `[task_labels]`, a task not the role's, or several task labels → no agent run, no attempt counted: the router subscribes you, moves the issue to In Review and comments why; fix the label, move it back to Todo. To upgrade a Light Research issue, swap in the Deep Research label, comment the claims to verify and move it back to Todo: the next agent run is `deep-research`, with the earlier report in its input. A resumed agent run keeps its session's task whatever the labels say (`<work_dir>/logs/runs.jsonl` keeps only the role); an interrupted agent run whose logged role is not the assignee's goes to In Review with a comment.

**Upgrade** from task-level config: a role table holding `default_task` or `tasks` stops agent runs with a config error naming its new table. Drop `default_task`; move the keys of `core.local.toml`'s `[roles.<role>.tasks.<task>]` to `[roles.<role>]` and of its `[clients.<name>.roles.<role>.tasks.<task>]` to `[clients.<name>.roles.<role>]` (e.g. `[clients.skill.roles.<role>.output]`), of `orchestrator.local.toml`'s `[core.roles.<role>.tasks.<task>]` to `[core.roles.<role>]`. Then do Operating's Upgrade too.

## Operating

```mermaid
flowchart LR
    L[launchd] -->|every 30 min| R["router.py: claim or resume, render the input"] -->|"tmux, input as text"| I[run.py]
    Y[you] -->|"--issue, --tui"| R
    I --> D["drive.py: claude"] -->|"marks, outcome"| W["writeback.py → Linear, as the role"]
    L -->|every 5 min| P[promote.py] --> X[prune.py]
```

```bash
python3 orchestrator/src/router.py --now --dry-run           # the next tick's plan and usage probe; changes nothing
python3 orchestrator/src/router.py --now                     # a tick now, outside the schedule
python3 orchestrator/src/router.py --issue TASK-12           # start that Todo issue or resume that In Progress one now
python3 orchestrator/src/router.py --issue TASK-12 --dry-run # what --issue would do; changes nothing
python3 orchestrator/src/promote.py --dry-run                # what Handoff and prune would do
python3 orchestrator/src/promote.py --now                    # Handoff now, skipping the 10-minute undo window
tmux ls                                                      # running sessions, agent-pm-<role>-<ID>
tmux attach -t '=agent-pm-<role>-<ID>'                       # watch one agent run; = matches the exact name
```

`--issue` skips the hours, `max_runs` and usage gates and Recover's waits (`tmux ls` is the truth): a ready Todo issue is claimed as in a tick; an In Progress one resumes its session in `runs.jsonl`, or, with none to resume, goes where Recover sends it; an issue with a live agent run gets its attach command instead (exit 1). While another router runs, nothing happens (exit 1).

| Log | Contents |
|---|---|
| `<work_dir>/logs/orchestrator.jsonl` | Every router, promote, prune, run and write-back event, one JSON object a line: `ts`, `src`, `kind`, then `issue` when it has one. An idle tick writes none; the same skip or config-load error, once a day. launchd's crash output may add non-JSON lines |
| `<work_dir>/logs/runs.jsonl` | Agent run start/resume lines of the last 7 days: keep it, resume and Recover read it |

An agent run's workdir `<work_dir>/work/<ID>/` holds `run.jsonl` (its record: input, sessions, progress, outcome; headless, the client's stderr), `writeback.json` (the write-back steps done) and, for a `local` or `orchestrator` destination, `out.md` (the deliverable); `src/`, `publish/` and `tmp/` are workspaces (Finish). Documents are published from the agent run's clone in `publish/`: `git pull` your own docs clone to see them. Each session gets a `Run <sid>` comment on its issue, holding `cd <cwd> && claude --resume <sid>`: to open the session, first move the issue out of In Progress, or the router may resume it once idle 30 minutes. Moving `<work_dir>` or a run's cwd breaks resuming its in-progress agent runs; moving `<work_dir>` or the repo breaks the installed plists.

**Upgrade** to `orchestrator.jsonl` and `runs.jsonl`: deploy with no issue In Progress (nothing reads the old `runs.log`, so Recover would send such an issue back to Todo for a new session); reinstall both plists, whose log path changed, as for a schedule change (Setup); delete the old logs, no archive (another `<work_dir>`: its `logs/`):

```bash
rm -rf ~/.agent-pm/logs/runs.log ~/.agent-pm/logs/router.log ~/.agent-pm/logs/promote.log ~/.agent-pm/logs/projects ~/.agent-pm/logs/tui
```

### Attended runs

```bash
python3 orchestrator/src/router.py --issue TASK-12 --tui [--split right|below] [--split-from <session>] [--events <file>]
python3 orchestrator/src/router.py --now --tui [--split right|below] [--split-from <session>] [--events <file>]   # a tick
```

Watch an agent run in a TUI pane and step in: the same claim, input, write-back, session comment and log lines; launchd never passes `--tui`. `--tui` attends the agent run `--issue` (Operating) or a tick starts or resumes, and prints to stderr how to attach to the driver session `agent-pm-<role>-<ID>` (always detached) and the TUI session `<role>-<ID>-<sid[:8]>` (live run and `max_runs` slot: `CLAUDE.md` › Architecture), whose pane opens as a worker's does (`core/skills/tmux/SKILL.md` › Start), you the opener, or, outside a tmux grid, where `--split`/`--split-from` say. No pane to split (pass `--split-from`) or a bad `--events` file → nothing claimed, exit 2; a good one gets `HH:MM:SS <session> done|blocked|dead` lines. `tmux kill-session -t '=agent-pm-<role>-<ID>'` ends the agent run and its TUI session; the issue stays In Progress and Recover resumes it (`--issue`: at once). After the outcome or a give-up the TUI session stays open, nothing in it written back, until the issue's next agent run or `prune.py` (24 hours after Done or Canceled) closes it by name: give no other tmux session a `<role>-<ID>-<8 hex>` name.
