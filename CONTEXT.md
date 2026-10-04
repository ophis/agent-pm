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
One fresh `claude -p` session doing one task as one role.
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
An agent runtime a run executes in (Claude Code today); it turns a composed run into its own command and enforces the run's hard constraints.
_Avoid_: runtime, backend

**Deliverable**:
The Markdown a run delivers to its destination (report, PRD, PR description); the output file's body after the frontmatter.
_Avoid_: document, result, report

**Vehicle**:
A form a prompt is delivered in (skill, plugin agent, `claude -p` prompt); roles and tasks stay vehicle-neutral.

**Orchestration layer**:
The code that turns a Linear issue into free text, calls the delegate, publishes the output and writes back to Linear.
_Avoid_: runner
