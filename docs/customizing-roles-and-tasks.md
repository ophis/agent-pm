# Customizing roles and tasks

- The built-in roles, tasks and templates live in `delegate/src/agent_pm/delegate/team/`; don't edit them.
- A role's run settings (model, client, destination, commands) go in `~/.agent-pm/config.local.toml`.
- Your own roles, tasks and templates go in a team dir. A team dir only adds: it cannot change or replace a built-in role, task or template, so a new task needs a new role.
- Only researcher, pm and engineer run on the board ([On the board](#on-the-board)); your own roles run through `/agent-pm:act-as`, `/agent-pm:manage` and `agent-pm-delegate run`.

## See what exists

`agent-pm-delegate run --list` prints each role with its destination type and source file, then its tasks with theirs. It checks every team file and renders every role's prompt, writing nothing; exit 2 names the bad file or key.

## Change a role's run settings

Set run keys under `[delegate]` (every role) or `[delegate.roles.<role>]` (one role) in `~/.agent-pm/config.local.toml`. Each key, its values and the order they resolve in: the comments of `delegate/src/agent_pm/delegate/config.toml`.

```toml
[delegate.roles.engineer]
client = "codex"          # a [clients.<name>] of session/src/agent_pm/session/config.toml
tier = 1

[delegate.roles.researcher.output]
type = "github"
repo = "<owner>/<name>"
branch = "main"
dir = "Research/"
```

- **Client:** `client` picks it; `agent-pm-delegate run --client` overrides it for one run. The flags a role run passes its client: [Client policy](sessions.md#client-policy).
- **Claude account:** `[delegate.roles.<role>.accounts]` `claude = "work"` runs the role's Docker Claude sessions on that registered account, keyed by client: [Docker host › Claude accounts](docker-host.md#claude-accounts).
- **Destination:** `[output]` (`local`, `github`, `pull-request` or `orchestrator`); a lower level's `[output]` replaces the whole table.
- **Access:** `read`, `write` and `commands` widen what the agent run may read, write and run; `templates` inlines templates into its prompt.
- **Board runs:** the same keys under `[linear_orchestrator.core]` (e.g. `[linear_orchestrator.core.roles.engineer]`) apply to board runs only, after the `[delegate]` ones.

## Add roles, tasks and templates

1. Make a team dir, e.g. `~/.agent-pm/team/`, holding any of `roles/`, `tasks/`, `templates/` and `tools/`. Keep it out of every dir a run can write (below).
2. List it in `~/.agent-pm/config.local.toml` (only there: the packaged `config.toml` refuses `team_dirs`):
   ```toml
   [delegate]
   team_dirs = ["~/.agent-pm/team"]
   ```
3. Write the role and task files (formats below), named `<name>.md` with `<name>` of `[a-z0-9-]`.
4. Check with `agent-pm-delegate run --list`, then [run it](#run-and-check).

Rules a team dir follows; each break stops the run with an error naming the file:

- **Add-only:** a role, task or template name that core or an earlier team dir already defines.
- **Own tasks:** a role lists only tasks in its own team dir's `tasks/`; a built-in role only built-in tasks.
- **Templates:** any role may name any template, built-in or yours, in `templates`.
- **Text only:** no front matter (`---`) and no `{{…}}` placeholder; a file may name a method only as a built-in `<team>/methods/<m>.md`.
- **Tools:** a file in `tools/` runs as `{{role_team}}/tools/<file>` in the role's `commands` (written `<role team>/tools/<file>` in its text).
- **Out of reach:** the team dir, its files and its `tools/` tree must not overlap the run's workdir, cwd, a write dir, a dir the run adds or a dir the client can write (Codex: its `CODEX_HOME`). Codex's default `danger-full-access` ignores this: [Client policy](sessions.md#client-policy).

A team dir's role needs no `[delegate.roles.<role>]` table: unset keys take the `[delegate]` values.

### Role file: `roles/<role>.md`

A `# ` title, an intro, then `## Tasks`, the role's only task list. Its first line names the default task as `unsure → `<task>` (`<tasks>/<task>.md`)`; then one line per task. Each path is exactly `<tasks>/<task>.md`, and the default is a listed task.

```markdown
# Reviewer

You review a change against its requirement.

## Tasks

Pick the task below that fits the input; unsure → `review` (`<tasks>/review.md`).

- `review` (`<tasks>/review.md`): review a pull request against its issue.
```

### Task file: `tasks/<task>.md`

It starts with a `# ` heading, has exactly one `[agent-pm-progress:start] <what to report>` line and a `## Resume` section, outside code fences. The smallest built-in example: `delegate/src/agent_pm/delegate/team/tasks/echo.md`.

## Run and check

| Command | Does |
|---|---|
| `agent-pm-delegate run --list` | Lists and checks every role and task. |
| `agent-pm-delegate run --role <role> [--task <task>] --input <text> --dry-run` | Prints the resolved client, tier, effort, cwd, dirs, commands and prompt as JSON; starts nothing. |
| `agent-pm-delegate prompt --role <role> [--task <task>] --input <text> --workdir <dir> --out <file>` | Writes the role's prompt to `<file>`; starts nothing. |
| `/agent-pm:act-as <role>[:<task>] <input>` | Runs the role in this conversation. |

## On the board

The router's per-role behavior is code: `TASKS` in `linear-orchestrator/src/agent_pm/linear_orchestrator/engine/config.py` has researcher, pm and engineer. A `[linear_orchestrator.roles.<role>]` table for any other role stops every `agent-pm-linear` command with `has no entry in config.TASKS`, and a `[linear_orchestrator.task_labels]` or `[linear_orchestrator.task_tiers]` entry must name a task of an orchestrated role. Board runs' run keys: `[linear_orchestrator.core]`, above.
