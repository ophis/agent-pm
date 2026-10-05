# Agent PM

An unattended pipeline that runs role agents on tasks, split into a core pack that turns free text into Markdown and an orchestration layer that connects it to Linear and the docs repo.

## Language

**Role**:
A persona with standards and boundaries (researcher, pm, engineer), defined by a charter plus config.
_Avoid_: agent, persona

**Task**:
One job a role can do (deep research, light research, product design, engineering), defined by steps plus run config.
_Avoid_: skill, job

**Principles**:
Rules every role follows; on conflict, principles > charter > task.

**Charter**:
A role's text: the rules shared by all of that role's tasks.

**Run**:
One task done once as one role: a fresh client session (e.g. Claude Code's) hosted by a runner, or inline in the caller's session through a skill.
_Avoid_: job, subagent

**Core pack**:
The roles, tasks, templates and delegate, packaged as a Claude Code plugin; knows nothing of Linear or where documents are stored.
_Avoid_: core skill

**Delegate**:
The component that turns a role, a task and free-text input into a run: a composer plus a driver.

**Composer**:
The part of the delegate that assembles principles, role, task and input into a prompt and run config.

**Driver**:
The part of the delegate that starts a run through a client and checks its output; the same for every client.

**Client**:
A way to execute a composed run (Claude Code, a skill); it turns the run into its own launch (a command per runner, or files) and enforces what hard constraints it can.
_Avoid_: runtime, backend

**Runner**:
How the driver hosts a client's command and when the run counts as done: headless (e.g. `claude -p`, on a pipe, done when it exits) or tui (the client's interactive command in a tmux session, done once the outcome arrives or it gives up, the session left open).

**Attended run**:
A run started by hand with the tui runner so you watch it in a TUI pane and can step in; otherwise the same claim, input, write-back and lock as an unattended run. Its TUI session outlives it and is closed by the issue's next run or by prune.

**Destination**:
Where a run delivers its deliverable: a GitHub repo, a local file, a pull request, or back to the orchestrator that called it.

**Deliverable**:
The Markdown a run delivers to its destination (report, PRD, PR description): returned in its outcome or published by the run, per the destination.
_Avoid_: document, result, report

**Vehicle**:
A form a prompt is delivered in (skill, plugin agent, `claude -p` prompt); roles and tasks stay vehicle-neutral.

**Orchestration layer**:
The code that turns a Linear issue into free text, calls the delegate and maps the run's outcome back to Linear; the orchestrator of the pipeline's runs.
_Avoid_: runner

**Write-back**:
The orchestration layer's mapping of a run's start and progress marks and its outcome to Linear (start, progress, finish).

**Overlay**:
The core run keys the orchestrator adds to its own runs, in `orchestrator/config.toml`'s `[core]` table, applied after the client's config.
