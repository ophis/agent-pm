# Agent PM

An unattended pipeline that runs role agents on tasks, in five layers (README › Core architecture).

## Language

**Layer**:
One project of the stack and its package, `<project>/src/agent_pm/<package>/`; import and command boundaries: `AGENTS.md` › Import rules.
_Avoid_: level (a config override level)

**Role**:
A persona with standards and boundaries (researcher, pm, engineer, dummy-tester), defined by a charter plus config.
_Avoid_: agent, persona

**Task**:
One job a role can do (deep research, research, light research, product design, build, light build, echo), defined by its task file's steps and listed in its role's task index.
_Avoid_: skill, job

**Principles**:
Rules every role follows; precedence: `delegate/src/agent_pm/delegate/team/guide.md`.

**Charter**:
A role's text: its task index and the rules shared by all of that role's tasks.

**Team dir**:
A dir listed in core config's `team_dirs`, outside the plugin, adding roles (each with its own tasks) and templates to the core pack's, never replacing or extending them.
_Avoid_: custom pack

**Agent run**:
One role run once, on a task given or picked from its index: a fresh client session (e.g. Claude Code's) run by the session layer in a mode, or inline in the caller's session through a skill.
_Avoid_: job, subagent

**Core pack**:
The roles, tasks, templates and delegate, packaged as a Claude Code plugin.
_Avoid_: core skill

**Delegate**:
The component that turns a role, an optional task and free-text input into an agent run: it has the composer build the prompt, runs it through the session layer, reads the agent run's replies (progress, outcome) and decides nudges and give-ups. In code: `delegate/src/agent_pm/delegate/`.

**Driver**:
The Delegate interface for starting a prompt: `AgentDriver` starts a Session through its configured class; `InstructionDriver` returns an Instruction file directly. In code: `delegate/src/agent_pm/delegate/engine/drivers.py`.

**Harness**:
The process that runs one detached Delegate run to its end as session `<name>-drive` on its harness Host, the run's state kept in its run dir, `<workdir>/run/`. In code: `delegate/src/agent_pm/delegate/engine/harness.py`; the run dir: `engine/state.py`.

**Composer**:
The component that assembles principles, role and input into a prompt (no task text: the agent run reads its task file) and resolves the role's run config.

**Session layer**:
The component that runs a prompt on the client and mode its caller names, through that Client: it starts or resumes the session, sends messages (tui only), times inactivity, and passes events and replies up without interpreting them; the same for every client. In code: `session/src/agent_pm/session/`; its users import `agent_pm.session`, Clients and Hosts written outside it `agent_pm.session.extension`.
_Avoid_: facade, runtime (a Mode's class), harness

**Client**:
The agent program a session runs (Claude Code, Codex, a test fake), and the code that wraps it for the session layer: its launch per mode, hooks, reply reading and message sending; everything client-specific lives in it. Named by its `[clients.<name>]` table. In code: the ABC `Client` (`session/src/agent_pm/session/clients/`); its name: `Spec.client`, the run key `client`, `--client` and records' `client` field.
_Avoid_: adapter, runtime (a Mode's class), agent (a role's run), backend, vehicle

**Mode**:
How the session layer hosts a client: headless (e.g. `claude -p`, on a pipe, ends when it exits), tui (its interactive command in a terminal on its Host, ends when its caller stops it) or in_process (the Client's run inside the caller's process, no process started, ends when the run returns). In code: `Mode` (`mode = headless/tui/in_process`), the Delegate CLI's `--mode`; its Runtime runs it.
_Avoid_: runtime (the class that runs it)

**Runtime**:
The session layer class that runs a session in one Mode: starts the client's process or thread (a tui one can also attach to a live session), polls its events, sends to it and stops it. In code: the ABC `Runtime` (`session/src/agent_pm/session/runtimes/base.py`) and `HeadlessRuntime`, `TuiRuntime` and `InProcessRuntime` (`runtimes/`).
_Avoid_: mode (the value it runs), runner

**Session**:
One client session the session layer runs from a Spec, in a Mode, on a Host, known by its sid. In code: the ABC `Session` (`session/src/agent_pm/session/engine/base.py`) and its one concrete class, `HostedSession` (`engine/hosted_session.py`).
_Avoid_: agent run (a role's run, in a session)

**Spec**:
What a caller asks the session layer to run: workdir, Access, tier, effort, flags, env, the sid to resume. In code: `session/src/agent_pm/session/engine/types.py`.

**Host**:
Where the session layer runs processes by name (e.g. in tmux sessions, or each on a pty its own supervisor holds). A Showable Host also shows them and reads their screen. In code: `session/src/agent_pm/session/hosts/`.

**Record**:
A versioned JSON line naming a started thing, its session identity and display, with commands to control and resume it. In code: `utils/src/agent_pm/utils/record.py`.

**Session status log**:
The line-per-event file a session's watchers read: `HH:MM:SS <name> <word> ...`. In code: `session/src/agent_pm/session/engine/status_log.py`.
_Avoid_: event

**Delegate event**:
The event a Delegate run delivers to its callback: Begun, Progress or Finished. In code: `delegate/src/agent_pm/delegate/engine/types.py`.
_Avoid_: event

**Attended run**:
An agent run started by hand in tui mode so you watch it in a TUI pane and can step in; otherwise the same claim, input and write-back as an unattended agent run.

**Destination**:
Where an agent run delivers its deliverable: a GitHub repo, a local file, a pull request, or back to the orchestrator that called it.

**Deliverable**:
The Markdown an agent run delivers to its destination (report, PRD, PR description): returned in its outcome or published by the agent run, per the destination.
_Avoid_: document, result, report

**Linear orchestrator**:
The layer that turns a Linear issue into free text, calls the delegate and maps the agent run's outcome back to Linear. In code: `linear-orchestrator/src/agent_pm/linear_orchestrator/`.
_Avoid_: orchestration layer, runner

**Issue**:
A Linear issue: the unit of work the Linear orchestrator claims for one role, and the place its agent runs report to. Chinese text says "issue" too.
_Avoid_: ticket, card, 票

**Write-back**:
The Linear orchestrator's mapping of an agent run's start and progress marks and its outcome to Linear. In code: `linear-orchestrator/src/agent_pm/linear_orchestrator/engine/writeback.py`.

**Overlay**:
The core run keys the Linear orchestrator adds to its own agent runs, in `linear-orchestrator/src/agent_pm/linear_orchestrator/config.toml`'s `[core]` table (its comment).

**Manager**:
The top layer: the commander session you talk to (`/agent-pm:manage`) and its crew. In code: `manager/src/agent_pm/manager/`.

**Crew**:
A manager's command executor. It owns a Roster, starts opaque argv and uses each Record's control and resume commands. In code: `manager/src/agent_pm/manager/engine/crew.py`; command entry: `manager/src/agent_pm/manager/cli.py`.

**Roster**:
A manager's ledger of launch intents and Records, entry states, holder lease and events cursor: `~/.agent-pm/managers/<name>/roster.json`. It alone writes the ledger and manages event rotation. In code: `manager/src/agent_pm/manager/engine/roster.py` and `events.py`.

**Worker**:
A Session-owned plain tui session, with its state in `~/.agent-pm/workers/<sid>/worker.json`; no role, no task. Started, restarted and resumed by `agent-pm-session`, which prints its Record. In code: `session/src/agent_pm/session/engine/worker.py`.
_Avoid_: subagent
