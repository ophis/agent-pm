# Agent PM

An unattended pipeline that runs role agents on tasks, split into a core pack that turns free text into Markdown and an orchestration layer that connects it to Linear and the docs repo.

## Language

**Role**:
A persona with standards and boundaries (researcher, pm, engineer, dummy-tester), defined by a charter plus config.
_Avoid_: agent, persona

**Task**:
One job a role can do (deep research, light research, product design, build, light build, echo), defined by its task file's steps and listed in its role's task index.
_Avoid_: skill, job

**Principles**:
Rules every role follows; precedence: `new/core/src/agent_pm/delegate/team/guide.md`.

**Charter**:
A role's text: its task index and the rules shared by all of that role's tasks.

**Team dir**:
A dir listed in core config's `team_dirs`, outside the plugin, adding roles (each with its own tasks) and templates to the core pack's, never replacing or extending them.
_Avoid_: custom pack

**Agent run**:
One role run once, on a task given or picked from its index: a fresh agent runtime session (e.g. Claude Code's) run by the driver in a mode, or inline in the caller's session through a skill.
_Avoid_: job, subagent

**Core pack**:
The roles, tasks, templates and delegate, packaged as a Claude Code plugin.
_Avoid_: core skill

**Delegate**:
The component that turns a role, an optional task and free-text input into an agent run: it has the composer build the prompt, runs it through the driver, reads the agent run's replies (progress, outcome) and decides nudges and give-ups. In code: `new/core/src/agent_pm/delegate/`.

**Composer**:
The component that assembles principles, role and input into a prompt (no task text: the agent run reads its task file) and resolves the role's run config.

**Driver**:
The component that runs a prompt on the agent runtime and mode its caller (the delegate, or a crew manager for a generic worker) names, through that runtime's adapter: it starts or resumes the session, sends messages (tui only), times inactivity, and passes events and replies up without interpreting them; the same for every runtime. In code: `new/core/src/agent_pm/session/` (`engine/api.py`, `modes/`).
_Avoid_: facade, runtime, harness

**Adapter**:
The code that wraps one agent runtime (Claude Code, Codex) for the driver: its launch per mode, hooks, reply reading and message sending; everything runtime-specific lives in it. In code: `new/core/src/agent_pm/session/adapters/`.
_Avoid_: client, backend, vehicle

**Mode**:
How the driver hosts an agent runtime: headless (e.g. `claude -p`, on a pipe, ends when it exits), tui (its interactive command in a tmux session, ends when its caller stops it) or in_process (the Adapter's run inside the caller's process, no process started, ends when the run returns). In code: `--runner` (renamed by TASK-310).
_Avoid_: runner

**Host**:
Where the session layer runs processes by name (e.g. in tmux sessions), keeping each one's Record and display; only a Displayable Host can show them. In code: `new/core/src/agent_pm/session/hosts/base.py`.

**Record**:
One JSON line naming a started thing (a worker, a role run, a pipeline) by its outermost starter: how to query it (its `ctl` command) and resume it (its `resume` command). In code: `new/core/src/agent_pm/utils/record.py`.

**Session status log**:
The line-per-event file a session's watchers read: `HH:MM:SS <name> <word> ...`. In code: `new/core/src/agent_pm/session/engine/status_log.py`.
_Avoid_: event

**Delegate event**:
The callback line a Delegate run emits: begun, progress, finished. In code: `new/core/src/agent_pm/delegate/event/delegate_event.py`.
_Avoid_: event

**Attended run**:
An agent run started by hand in tui mode so you watch it in a TUI pane and can step in; otherwise the same claim, input and write-back as an unattended agent run (README › Attended runs).

**Destination**:
Where an agent run delivers its deliverable: a GitHub repo, a local file, a pull request, or back to the orchestrator that called it.

**Deliverable**:
The Markdown an agent run delivers to its destination (report, PRD, PR description): returned in its outcome or published by the agent run, per the destination.
_Avoid_: document, result, report

**Orchestration layer**:
The code that turns a Linear issue into free text, calls the delegate and maps the agent run's outcome back to Linear (the Linear orchestrator); the orchestrator of the pipeline's agent runs. Elsewhere: a control plane.
_Avoid_: runner

**Write-back**:
The orchestration layer's mapping of an agent run's start and progress marks and its outcome to Linear.

**Overlay**:
The core run keys the Linear orchestrator adds to its own agent runs, in `new/linear-orchestrator/config.toml`'s `[core]` table (its comment).
