# agent-pm

[![test](https://github.com/ophis/agent-pm/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/ophis/agent-pm/actions/workflows/test.yml?query=branch%3Amain)

Runs Claude agents unattended from a Linear board. Each Linear project is a product; an issue's assignee, a role account, is its stage. Each role works up to `max_runs` issues at once, handing its output back for your review; once you approve, the next role takes it. The core runs a role, which picks its task unless given one, and is a Claude Code plugin you can use alone (Install); the Linear orchestrator runs it from the board (Setup). Their code: Layout; local headless Codex role runs: Client policy. Developers: `AGENTS.md`.

```mermaid
flowchart LR
    R["researcher<br>deep-research, light-research"] -->|"report → Approve → promote"| P["pm<br>product-design"]
    P -->|"PRD → Approve → promote"| E["engineer<br>build, light-build"] -->|pull request| Y[you]
    T[agent-pm-linear router] -.->|claims Todo issues| R & P & E
```

### Core architecture

Five layers, one project each: utils < session < delegate < linear-orchestrator < manager. Import rules: `AGENTS.md` › Import rules. The code, by package; terms in `CONTEXT.md`.

```mermaid
flowchart TB
  subgraph EN[entry points]
    LD[launchd]
    HU[the user]
    CC[Control/resume commands]
  end
  subgraph MG[agent_pm.manager]
    W["engine/crew.py"] --> RS["engine/roster.py"]
  end
  subgraph LO[agent_pm.linear_orchestrator]
    LCLI["cli.py<br>agent-pm-linear"]
    R["engine/router.py<br>pick · usage gate · trigger"]
    LR["engine/linear_run.py<br>Delegate ownership · queries"]
    WB["engine/writeback.py<br>one event's write-back"]
    PP["engine/promote.py → prune.py"]
    LC["engine/config.py<br>[core] overlay"]
  end
  subgraph DL[agent_pm.delegate]
    D["cli.py<br>agent-pm-delegate"]
    HD["engine/harness_delegate.py<br>HarnessDelegate · state.py RunState"]
    H["engine/harness.py<br>detached run"]
    E["engine/: base · plan · drivers · runner · callback · types"]
    C["composer/<br>prompt · run spec"]
    EV["event/<br>reply · outcome check · callback line"]
    CF["config.py<br>package settings"]
    subgraph TM[team/]
      T["roles · tasks · templates"]
      AT[tools/]
    end
  end
  subgraph SE[agent_pm.session]
    SC["cli.py<br>agent-pm-session"]
    DR["engine/: base · hosted_session · worker · types · records<br>modes/: headless · tui · in_process<br>hosts/tmux/: host · transport · placement · grid"]
    subgraph AD[adapters/]
      CL[claude]
      CX[codex]
    end
  end
  U[agent_pm.utils]
  subgraph RT[clients]
    P1[Claude Code]
    P2[Codex CLI]
  end
  LD -.->|"router · promote"| LCLI
  HU -.->|"router --issue"| LCLI
  HU -.->|"act-as · CLI"| D
  HU -.-> W
  HU -.-> SC
  SC --> DR
  LCLI --> R & PP & WB
  R --> LR
  LR -->|"agent_pm.delegate: detach, views"| HD
  H -.->|"each event: agent-pm-linear writeback"| LCLI
  LC -->|"Delegate class methods"| HD
  W -.-> CC
  D & H --> HD
  HD -.->|"detach on the harness Host"| H
  HD --> E & C
  E --> C
  DL -->|agent_pm.session| SE
  C -->|reads| T
  DR --> AD
  CL -.-> P1
  CX -.-> P2
  RT -.-> AT
  classDef shared stroke-dasharray: 4 3
  class EV,U,CF shared
```

Solid arrow: an import, the only direction allowed, or a file read (`reads`); its label names the package imported, by its `__all__`. Dotted: runs a command or process. Dashed: shared modules, their imports not drawn.

## Install

Before upgrading, close running agent-pm sessions: earlier sessions lack generation-scoped terminal controllers.

`/plugin marketplace add ophis/agent-pm`, then `/plugin install agent-pm@agent-pm` (a private repo: your git credentials). Requires the `claude` CLI, Python 3.11+ (stdlib only), tmux 3.3+ (tui runs), `git` and `gh` logged in (checkouts, the `github` and `pull-request` destinations), the `autopilot` plugin (plus `superpowers` for `build`), GitHub and the web; `osascript`, `pgrep` and a running iTerm2 only for the default iTerm2 split. Nothing checks them: a missing one fails the agent run. Agent runs load your user settings (`~/.claude/CLAUDE.md`, skills, permissions, plugins but `agent-pm`) and no MCP servers; a trusted cwd adds its project settings, `CLAUDE.md`, skills and `.mcp.json` (`delegate/src/agent_pm/delegate/config.toml`'s `cwd` and `trusted_dirs`).

- `/agent-pm:tmux`: removed, rebuilt by [TASK-356](https://linear.app/ophis-workgroup/issue/TASK-356).
- `ctmux <name>` (`manager/skills/tmux/scripts/ctmux`): the commander's tmux session `<name>`, attached through iTerm2's tmux integration (`-CC`). A new session's shell runs under `agent-pm-crew hold --manager=<name>`, which holds that manager's lease while the shell lives: start the commander's client in it. `agent-pm-crew` must be on the tmux server's PATH. `ctmux --install` copies the script to `~/.agent-pm/bin/ctmux` and prints that path.
- `/agent-pm:manage`: coordinate the commander session through Crew; command guidance: `manager/skills/manage/SKILL.md`.
- `/agent-pm:act-as <role>[:<task>] <input>`: run a core role in this conversation, on that task or one it picks: `delegate/skills/act-as/SKILL.md`.
- Your own roles, tasks, templates and tools, outside the plugin: `delegate/src/agent_pm/delegate/config.toml`'s `team_dirs` comment; `agent-pm-delegate run --list` (`delegate/src/agent_pm/delegate/cli.py`) lists every role and task.

## Setup

Requires macOS, Install's requirements and `gh` logged in as you. The Linear orchestrator runs this checkout's scripts, not the plugin.

1. Create `~/.agent-pm/config.local.toml` from the projects' `# local:` examples, placing each fragment under its layer's section as shown below. Include researcher's and pm's `[output]` (delegate's Destination comment); a required key left `""` stops every `agent-pm-linear` command. `<work_dir>` below is `linear_orchestrator.work_dir`. Merge and validation rules: `utils/src/agent_pm/utils/config_file.py`'s docstring.
2. Find `task_label_group` with the Linear API's `issueLabels { nodes { id name isGroup } }`; the router stops if it isn't a label group or a `[task_labels]` id isn't one of its labels.
3. Agent runs need no `linear` skill and get no Linear key: router and promote act in Linear as the harness account `frank.agent.w@gmail.com`, each role's write-back as its `[roles.<role>]` `account`, its API key in the Keychain under `key`. Per role:
   1. Linear → Settings → Members: invite the `account` (a Gmail plus-alias of the harness account, e.g. `frank.agent.w+pm@gmail.com`).
   2. Open the invite in a private window, choosing **Continue with email** (Google signs in as the harness account).
   3. As the role account, create a personal API key in its settings.
   4. `security add-generic-password -s <key> -a <account> -w`, pasting the key at the prompt. `agent-pm-linear router` checks the item exists; a missing one stops that role's agent runs with a `config-error` event in `<work_dir>/logs/orchestrator.jsonl`.
4. The harness account's key:
   ```bash
   security add-generic-password -a frank.agent.w -s linear-api-key -w   # config.local.toml: [linear_orchestrator] harness_key
   ```

launchd scheduling of router and promote returns with [TASK-379](https://linear.app/ophis-workgroup/issue/TASK-379).

### Local configuration migration

Flat local files now fail validation. Move their top-level values into `[session]`, `[delegate]` or `[linear_orchestrator]`, according to the layer's packaged `config.toml`. Prefix every table with that section: `[roles.engineer]` becomes `[delegate.roles.engineer]` for run settings, and `[linear_orchestrator.roles.engineer]` for board settings. Keep values and list contents unchanged. For a key defined by several layers, set it in every layer whose standalone reader needs the override; an upper packaged value takes precedence over a lower local value. A `show` key belongs under `[delegate]` or `[delegate.roles.<role>]`. Old `core-new.local.toml` and `linear-new.local.toml` fragments belong under `delegate` and `linear_orchestrator` respectively. Migration is manual; no local file is rewritten.

```toml
[session.hosts.tmux]
status_line = true

[delegate]
language = "English"

[delegate.roles.engineer]
tier = 2

[linear_orchestrator]
work_dir = "~/.agent-pm"

[linear_orchestrator.roles.engineer]
account = "engineer@example.com"
max_runs = 1
```

The run key `runtime` is renamed to `client`: set `client` wherever a local file sets `runtime` (`[delegate]`, `[delegate.roles.<role>]`, `[linear_orchestrator.core]` and its role tables); a `runtime` key left there is a config error naming `client`. `--runtime` options are `--client`, and `runs.jsonl` rows, worker states and tmux sessions recorded with `runtime` are not read.

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

The issue's label in the `Tasks` label group names one of the assignee role's tasks by its id, through `[task_labels]`, so renaming a label keeps working: the agent run does exactly that task. No such label → the agent run picks one from its role's task index (`delegate/src/agent_pm/delegate/team/roles/<role>.md` › Tasks: what each task does, when to use it, how heavy; unsure → the default it names), and its start comment names the pick and why. A label not in `[task_labels]`, a task not the role's, or several task labels → no agent run, no attempt counted: the router subscribes you, moves the issue to In Review and comments why; fix the label, move it back to Todo. To upgrade a Light Research issue, swap in the Deep Research label, comment the claims to verify and move it back to Todo: the next agent run is `deep-research`, with the earlier report in its input. A resumed agent run keeps its session's task whatever the labels say; an interrupted agent run whose logged role is not the assignee's goes to In Review with a comment.

**Upgrade** from task-level config: a role table holding `default_task` or `tasks` stops agent runs with a config error naming its new table. Drop `default_task`; move the keys of `config.local.toml`'s `[delegate.roles.<role>.tasks.<task>]` to `[delegate.roles.<role>]` and of its `[delegate.clients.<name>.roles.<role>.tasks.<task>]` to `[delegate.clients.<name>.roles.<role>]` (e.g. `[delegate.clients.skill.roles.<role>.output]`), of its `[linear_orchestrator.core.roles.<role>.tasks.<task>]` to `[linear_orchestrator.core.roles.<role>]`. Then do Operating's Upgrade too.

## Operating

```mermaid
flowchart LR
    L[launchd] -->|every 30 min| R["agent-pm-linear router: claim or resume, render the input"] -->|"detach its LinearRun, exit"| I["harness: Delegate, claude"]
    Y[you] -->|"--issue"| R
    I -->|"each event: agent-pm-linear writeback"| W["writeback.py → Linear, as the role"]
    L -->|every 5 min| P[agent-pm-linear promote] --> X[prune]
```

Run `bin/agent-pm-linear router …`, `… promote …` or `… prune …` (each `--help` lists its options) by its absolute path: it sets its own `PYTHONPATH`, and the runs' PATH holds this checkout's `bin/`, so a run's harness finds `agent-pm-linear writeback` under launchd's PATH.

```bash
tmux ls                                                      # running sessions: <role>-<ID>-<sid8>-drive (a run's harness), <role>-<ID>-<sid8> (its tui)
bin/agent-pm-linear attach-command <ID>                      # the command that shows one agent run
```

`--issue` skips the hours, `max_runs` and usage gates and Recover's waits (`tmux ls` is the truth): a ready Todo issue is claimed as in a tick; an In Progress one resumes its session in `runs.jsonl`, or, with none to resume, goes where Recover sends it; an issue with a live agent run gets its attach command instead (exit 1). While another router runs, nothing happens (exit 1).

| Log | Contents |
|---|---|
| `<work_dir>/logs/orchestrator.jsonl` | Every router, promote, prune and write-back event, one JSON object a line: `ts`, `src`, `kind`, then `issue` when it has one. An idle tick writes none; the same skip, config-load error or Linear outage (`linear-error`: a 5xx, rate limit, timeout or network error; the run stops, the next retries), once a day |
| `<work_dir>/logs/runs.jsonl` | Start/resume rows from the last 7 days, every issue's latest session rows regardless of age, and unexpired usage limits. Rows record Delegate kind and client; a start row also the claim's issue uuid, project, target and the role's key, which the run's write-back reads; legacy rows use the fixed `harness` registration. Keep it: resume, Recover and write-back read it |

An agent run's workdir `<work_dir>/work/<ID>/` holds `run.jsonl` (its record: input, sessions, progress, outcome; headless, the client's stderr), `delegate.json` (the Delegate's state, published from the State dir `~/.agent-pm/delegates/`), `writeback.json` (each session id's write-back steps done and each attempt's `Run <sid>` comment start and number) and, for a `local` or `orchestrator` destination, `out.md` (the deliverable); `src/`, `publish/` and `tmp/` are workspaces (Finish). Documents are published from the agent run's clone in `publish/`: `git pull` your own docs clone to see them. Each session gets a `Run <sid>` comment on its issue, holding `cd <cwd> && claude --resume <sid>`: to open the session, first move the issue out of In Progress, or the router may resume it once idle 30 minutes. Moving `<work_dir>` or a run's cwd breaks resuming its in-progress agent runs.

**Upgrade** to the trigger-only router: deploy with no issue In Progress; a run started before has no claim context in its start row, so its write-back refuses its events.

**Upgrade** to `orchestrator.jsonl` and `runs.jsonl`: deploy with no issue In Progress (nothing reads the old `runs.log`, so Recover would send such an issue back to Todo for a new session); delete the old logs, no archive (another `<work_dir>`: its `logs/`):

```bash
rm -rf ~/.agent-pm/logs/runs.log ~/.agent-pm/logs/router.log ~/.agent-pm/logs/promote.log ~/.agent-pm/logs/projects ~/.agent-pm/logs/tui
```

### Attended runs

A `tui` run's give-up timeout, its default and where to set it: `delegate/src/agent_pm/delegate/config.toml`'s `idle_timeout` comment. How a started `tui` session with a display is shown (a pane, only its attach command, or a command of yours): the same file's `show` comment.

The old router's `--tui` is gone; the new router gains it in [TASK-332](https://linear.app/ophis-workgroup/issue/TASK-332).

## Layout

```text
delegate/             the Delegate (one role's run), the Composer, the built-in team; shared event/ and config.py
session/              the session: an Adapter per client, Runtimes, Hosts (tmux); cli.py: agent-pm-session
utils/                shared utilities
manager/              cli.py, engine/crew.py, roster.py and events.py: commands and bookkeeping
linear-orchestrator/  cli.py: router, run, promote, run queries, writeback; engine/: scheduling, client, log, ops, issues, run_comments, writeback
tests/                repo-wide checks: guard_checks/ (rules), guard_data/ (each layer's exceptions), checks.py (runner), case.py and suites
.github/              CI workflows and their helpers
```

The Delegate's `Driver`, `AgentDriver` and `InstructionDriver` live in `delegate/src/agent_pm/delegate/engine/drivers.py`; definitions: `CONTEXT.md` › Driver.

Each project holds `src/agent_pm/<its package>/` (one PEP 420 namespace package `agent_pm` over the five `src/` roots) and `tests/`. Session, Delegate and orchestration configuration ships inside their packages. Each module's docstring states its behaviour.

## agent-pm-session

`bin/agent-pm-session` is the session layer's command (`--help` lists its subcommands and each Host's
`--display` format). `start NAME --client R [--cwd D] [--resume SID] [--prompt T] [--env K=V]…
[--display D] [-- FLAGS…]` starts a worker, a plain tui session of client R with its state in
`~/.agent-pm/workers/<sid>/worker.json`; it prints the worker's Record once the client has run 5 s
without exiting. Its session id is `--resume SID`, else `$AGENT_PM_SID`, else a new one; the two
must agree. There is no default client. `resume NAME` prints a live worker's Record and starts
nothing, else resumes it; `restart NAME` restarts it in place; both wait for the old client's
process group to exit first (10 s, then a kill). `send NAME --key K…` takes keys as one character
or `Enter`, `Escape`, `Tab`, `BTab`, `BSpace`, `Up`, `Down`, `Left`, `Right`, `Home`, `End`,
`PageUp`, `PageDown`, `F1`–`F12`, after any of `C-`, `M-`, `S-`.

Plain `ls` prints a header line, then one tab-separated row per session: `name`, `state`
(`working`, `done`, `blocked` or `dead`), `client`, `sid` and `display`, `-` for none. `ls
--records` prints only the Records sessions hold, one JSON line each; `--here` lists the sessions
the caller opened (`--descendants`: and those they opened, transitively).

## Client policy

Delegate reads `[policy.<client>]` `flags` (an argv list) and `env` (a string table) from core config with the local overlay, then supplies them as `Spec.flags/env`. Plain workers use their session Spec's flags and env. Every session then takes the same path: Adapters add client dialogue options, `[adapters.<client>]` keys and Access grants (workers included; a Claude worker's Access gives it `--setting-sources user,project,local`, Claude's default); the session layer owns session selection and the child env: the caller's, minus Git's repository selectors (`git rev-parse --local-env-vars`), minus every Adapter's identity markers, and minus an Adapter's injected defaults (Codex: `NO_COLOR`, `CODEX_CI`, `PAGER`, `GIT_PAGER`, `GH_PAGER`) only inside that client and at the injected value (`session/src/agent_pm/session/modes/base.py` `identity_drop`), under the Launch env.

The default policy sets Claude `--permission-mode auto`, `--strict-mcp-config` and a `--settings` turning this core's plugin off (the tui hooks merge into it), and Codex `-s danger-full-access`, `approval_policy="on-request"`, `approvals_reviewer="auto_review"`. To use Codex `workspace-write`, replace its policy flags with `-s workspace-write`, the approval options, `-c sandbox_workspace_write.network_access=false`, and `-c sandbox_workspace_write.writable_roots=[]`. Full access does not enforce writable-root or team-dir boundaries.

For existing `~/.agent-pm/config.local.toml` overrides, remove `[session.adapters.codex]` `sandbox` and `shell_network`: those keys now fail validation. Express them in `[delegate.policy.codex].flags` using the options above. Move Claude `--permission-mode` and `--strict-mcp-config` from `[session.adapters.claude].flags` into `[delegate.policy.claude].flags`; keep there only flags every session, workers included, should get. Policy flag lists replace the defaults wholesale, so repeat every option you intend to keep.

Codex snapshots save flags and the merged Adapter, Access and Spec environment. Resume replays them, ignoring current policy and new Spec flags/env; grants are also saved. The public resume command (run.jsonl, session comments) holds no environment values: it runs `session/src/agent_pm/session/adapters/codex/common/resume.py`, which reads them from the snapshot. Snapshots from before this contract fail with `unsupported legacy Codex recovery snapshot: start a new session`; they are not migrated. Access write directories are always emitted as `--add-dir`, including full-access runs.

## Access

A run's `read` dirs and its role's tasks dir are never `--add-dir`: no client is granted write to them. Codex has no read-only-dir mechanism usable here, so it adds nothing for them: under `workspace-write` its sandbox reads the whole disk and writes only its roots; under the default `danger-full-access` (Client policy, above) it can write them too. Each client's grant and its limits: `delegate/src/agent_pm/delegate/config.toml`'s `read` comment.

## Tests

```bash
export PYTHONPATH=.:.github:delegate/src:session/src:utils/src:manager/src:linear-orchestrator/src:linear-orchestrator/tests   # from the repo root
```

Run Lint checks with the pinned tool versions in `.github/workflows/test.yml`:

```bash
python3 .github/comment_budget.py
ruff check --no-cache
ruff format --check --no-cache
pyright
lint-imports --no-cache
pylint --disable=all --enable=cyclic-import,duplicate-code manager/src linear-orchestrator/src delegate/src session/src utils/src
vulture
python3 -m tests.checks
python3 .github/run_tests.py "Repository integration" tests/integration
python3 .github/integration_jobs.py --check
actionlint -shellcheck= -pyflakes=
typos --config pyproject.toml
```

One project's unit tests: `python3 .github/run_tests.py "Session unit" session/tests/unit`.
Run project and repository unit tests with `python3 .github/run_tests.py Unit --unit`; integration
shards with `python3 .github/integration_jobs.py <1–8>`. CI runs Lint, Unit and eight integration
shards; Lint also runs the repository integration tests with its pinned tools installed. Its
required `CI` job succeeds only when every dependency succeeds.

Unit tests exercise pure logic with fake objects and recorded output. They start no processes,
connect to no sockets, and write only inside the runner's temporary directory. Integration tests
exercise real tools through `utils/tests/sandbox.py`: controlled Python import roots, clean Git
environments and temporary repositories. Tmux uses the private servers in
`session/tests/live_tmux.py`; its explicit socket or `TMUX_TMPDIR` must resolve inside the sandbox.
`AGENT_PM_CONTROL_ROOT` puts ProcessControl directories in the sandbox; the gate fails one outside it.
A supervised run fails when a process naming its sandbox, or a sandbox tmux server, outlives it.
Fake and real implementations share contract assertions.

`.github/run_tests.py` resets HOME, temporary roots and Git configuration before supervision.
`.github/isolation.py` installs interpreter audit hooks and unit PATH blockers for git, tmux,
claude and codex. `tests/integration/unit_imports_integration_test.py` applies Ruff TID251 to unit
imports. The migration allowlist in `tests/guards.py` must remain empty; interpreter gates
permit no unit exemptions. Run tests through these supervised entries.
