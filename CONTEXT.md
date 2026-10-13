# Agent PM

An unattended pipeline that runs role agents on tasks, in five layers (README › Core architecture).

## Language

**Layer**:
One project of the stack and its package, `<project>/src/agent_pm/<package>/`; cross-layer imports use lower layers' public packages (AGENTS.md › Import rules).
_Avoid_: level (a config override level)

**Role**:
A persona with standards and boundaries (researcher, pm, engineer, dummy-tester), defined by a charter plus config.
_Avoid_: agent, persona

**Task**:
One job a role can do (deep research, light research, product design, build, light build, echo), defined by its task file's steps and listed in its role's task index.
_Avoid_: skill, job

**Principles**:
Rules every role follows; precedence: `delegate/src/agent_pm/delegate/team/guide.md`.

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
The component that turns a role, an optional task and free-text input into an agent run: it has the composer build the prompt, runs it through the driver, reads the agent run's replies (progress, outcome) and decides nudges and give-ups. In code: `delegate/src/agent_pm/delegate/`.

**Composer**:
The component that assembles principles, role and input into a prompt (no task text: the agent run reads its task file) and resolves the role's run config.

**Driver**:
The component that runs a prompt on the agent runtime and mode its caller (the delegate, or a crew manager for a generic worker) names, through that runtime's adapter: it starts or resumes the session, sends messages (tui only), times inactivity, and passes events and replies up without interpreting them; the same for every runtime. In code: `session/src/agent_pm/session/` (`engine/api.py`, `modes/`).
_Avoid_: facade, runtime, harness

**Adapter**:
The code that wraps one agent runtime (Claude Code, Codex) for the driver: its launch per mode, hooks, reply reading and message sending; everything runtime-specific lives in it. In code: `session/src/agent_pm/session/adapters/`.
_Avoid_: client, backend, vehicle

**Mode**:
How the driver hosts an agent runtime: headless (e.g. `claude -p`, on a pipe, ends when it exits), tui (its interactive command in a tmux session, ends when its caller stops it) or in_process (the Adapter's run inside the caller's process, no process started, ends when the run returns). In code: the Delegate CLI's `--mode`.
_Avoid_: runner

**Session**:
One agent runtime session the session layer runs from a Spec, in a Mode, on a Host, known by its sid. In code: `session/src/agent_pm/session/engine/api.py`.
_Avoid_: agent run (a role's run, in a session)

**Spec**:
What a caller asks the session layer to run: workdir, Access, tier, effort, flags, env, the sid to resume. In code: `session/src/agent_pm/session/engine/types.py`.

**Host**:
Where the session layer runs processes by name and displays them (e.g. in tmux sessions). In code: `session/src/agent_pm/session/hosts/`.

**Record**:
One roster entry naming what a manager started (a worker, a role run, a pipeline), its session identity, display state and resume command. In code: `manager/src/agent_pm/manager/engine/roster.py`.

**Session status log**:
The line-per-event file a session's watchers read: `HH:MM:SS <name> <word> ...`. In code: `session/src/agent_pm/session/engine/status_log.py`.
_Avoid_: event

**Delegate event**:
The callback line a Delegate run emits: begun, progress, finished. In code: `delegate/src/agent_pm/delegate/event/delegate_event.py`.
_Avoid_: event

**Attended run**:
An agent run started by hand in tui mode so you watch it in a TUI pane and can step in; otherwise the same claim, input and write-back as an unattended agent run (README › Attended runs).

**Destination**:
Where an agent run delivers its deliverable: a GitHub repo, a local file, a pull request, or back to the orchestrator that called it.

**Deliverable**:
The Markdown an agent run delivers to its destination (report, PRD, PR description): returned in its outcome or published by the agent run, per the destination.
_Avoid_: document, result, report

**Linear orchestrator**:
The layer that turns a Linear issue into free text, calls the delegate and maps the agent run's outcome back to Linear. In code: `linear-orchestrator/src/agent_pm/linear_orchestrator/`.
_Avoid_: orchestration layer, runner

**Write-back**:
The Linear orchestrator's mapping of an agent run's start and progress marks and its outcome to Linear.

**Overlay**:
The core run keys the Linear orchestrator adds to its own agent runs, in `linear-orchestrator/config.toml`'s `[core]` table (its comment).

**Manager**:
The top layer: the commander session you talk to (`/agent-pm:manage`) and its crew. In code: `manager/src/agent_pm/manager/`.

**Crew**:
A manager's workers and the commands that start, show, follow and stop them. In code: `manager/src/agent_pm/manager/engine/crew.py`.

**Roster**:
A manager's list of what it started (workers, role runs, pipelines), with its events cursor: `~/.agent-pm/managers/<name>/roster.json`. In code: `manager/src/agent_pm/manager/engine/roster.py`.

**Worker**:
A plain tui session a manager starts through its crew, in its own `~/.agent-pm/workers/<sid>/`; no role, no task.
_Avoid_: subagent
