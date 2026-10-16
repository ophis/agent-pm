# Sessions and client policy

How the sessions agent-pm starts are set up, how to list and show them, and the flags role runs pass their client.

## Session settings

Every session agent-pm starts, agent runs and workers, loads your user settings with every plugin except `agent-pm` (`[clients.<client>] flags` in `session/src/agent_pm/session/config.toml`): only a client you start, the commander, has `/agent-pm:tmux`. Each such session's client also carries `AGENT_PM_AGENT_SESSION=1`, under which `agent-pm-crew` `run`/`hold`/`resume`, `agent-pm-session` `start`/`restart`/`resume`/`client-resume`, `agent-pm-delegate` `run`/`resume` and `agent-pm-linear` `router`/`resume` exit 2 (their forms that start nothing, such as `--dry-run`, `--list` and `--brake`, still run), even run by a checkout's `bin/` path: a guard against slips, not isolation (`env -u` clears it). Agent runs load no MCP servers. A trusted cwd adds its project settings (`trusted_dirs` in `delegate/src/agent_pm/delegate/config.toml`).

## agent-pm-session

`bin/agent-pm-session --help` lists its subcommands. `ls` prints a header, then one tab-separated row per session: `name`, `state` (`working`, `done`, `blocked`, `dead`), `client`, `sid`, `display`, with `-` for an empty value.

Sessions run on `[session.hosts] default`: `tmux`, or `pty` (each on a pty its own supervisor holds, state in `~/.agent-pm/pty/<name>/`). A `pty` session cannot be shown: `show`, `hide`, `attach-command`, `read` and `tile` exit 1, and its `status` `attach` is null. `[delegate.delegates] harness_host = "pty"` puts only detached runs' harnesses there.

`start --display none` starts a worker without a pane, still opened by the caller, and hidden. Hidden (from that, `hide`, or closing its pane by hand; not from losing the manager's window or detaching its viewer) lasts through `restart` and `resume` until `show`.

## Client policy

Role runs' client flags are `[policy.claude]` and `[policy.codex]` in `delegate/src/agent_pm/delegate/config.toml`. A local list replaces the default whole. Codex defaults to `danger-full-access`, which ignores writable roots and team dirs. For `workspace-write`:

```toml
[delegate.policy.codex]
flags = ["-s", "workspace-write", "-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="auto_review"', "-c", "sandbox_workspace_write.network_access=false", "-c", "sandbox_workspace_write.writable_roots=[]"]
```
