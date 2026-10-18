# Sessions and client policy

How the sessions agent-pm starts are set up, how to list and show them, and the flags role runs pass their client.

## Session settings

Every session agent-pm starts, agent runs and workers, loads your user settings with every plugin except `agent-pm` (`[clients.<client>] flags` in `session/src/agent_pm/session/config.toml`): only a client you start, the commander, has `/agent-pm:manage`. Each such session's client also carries `AGENT_PM_AGENT_SESSION=1`, under which `agent-pm-crew` `run`/`worker`/`reopen`/`issue`/`role`/`hold`/`resume`, `agent-pm-session` `start`/`restart`/`resume`/`client-resume`, `agent-pm-delegate` `run`/`resume` and `agent-pm-linear` `router`/`resume` exit 2 (their forms that start nothing, such as `--dry-run`, `--list` and `--brake`, still run), even run by a checkout's `bin/` path: a guard against slips, not isolation (`env -u` clears it). Agent runs load no MCP servers. A trusted cwd adds its project settings (`trusted_dirs` in `delegate/src/agent_pm/delegate/config.toml`).

In the commander's Claude Code session the plugin's mod (`manager/hooks/quiet.tsx`) always hides Monitor tool-call rows and notices (each event, the expiry, the stream's end), plus Bash tool-call rows whose command contains `agent-pm-crew next-event` or `agent-pm-crew ack`. Other tools, Bash commands and notices draw as usual; the model's content is unchanged. Ctrl+o shows Monitor notices; the hidden tool-call rows stay hidden.

## agent-pm-session

`bin/agent-pm-session --help` lists its subcommands. `ls` prints a header, then one tab-separated row per session: `name`, `state` (`working`, `done`, `blocked`, `dead`, `unknown` when its host could not tell), `client`, `sid`, `display`, with `-` for an empty value.

Sessions run on `[session.hosts] default`: `tmux`, or `pty` (each on a pty its own supervisor holds, state in `~/.agent-pm/pty/<name>/`). A `pty` session cannot be shown: `show`, `hide`, `attach-command`, `read` and `tile` exit 1, and its `status` `attach` is null. `[delegate.delegates] harness_host = "pty"` puts only detached runs' harnesses there.

`start --display none` starts a worker without a pane, still opened by the caller, and hidden. Hidden (from that, `hide`, or closing its pane by hand; not from losing the manager's window or detaching its viewer) lasts through `restart` and `resume` until `show`.

## Client policy

Role runs' client flags are `[policy.claude]` and `[policy.codex]` in `delegate/src/agent_pm/delegate/config.toml`, after the session's `[clients.<client>] flags` and permission mode. A local list replaces the default whole.

Every session, crew workers and role runs, gets one permission mode from the Host it runs on: an `isolated` Host (`[hosts.docker] isolated = true`) `yolo`, every other Host `auto`. Each mode's flags are the client's, in `[clients.<client>.modes]` in `session/src/agent_pm/session/config.toml`, the only home of permission flags; a start, restart and resume each get the mode of the Host they run on, and `client-resume` (this machine) `auto`:

| Client | `auto` | `yolo` |
|---|---|---|
| Claude | `--permission-mode auto` | `--dangerously-skip-permissions --settings '{"skipDangerousModePermissionPrompt": true}'` (no first-start dialog) |
| Codex | `-s danger-full-access -c approval_policy="on-request" -c approvals_reviewer="auto_review"` | `--dangerously-bypass-approvals-and-sandbox` |
| OpenCode | none: its config's `permission.bash = "ask"` asks for every shell call but the run's pre-approved commands; headless refuses the call, the TUI shows the ask and the session is `blocked` | `--auto` |

OpenCode's pre-approved commands match a command's text without its redirections, so an allowed `git status` also allows `git status > <file>`. An OpenCode run edits only its cwd and Access dirs, never `.git` or OpenCode's own config, and reads its Access reads without editing them; a refused call does not end its turn. OpenCode matches these paths case-sensitively: on a case-insensitive disk (macOS's default) only `.git` is denied in every spelling, so `OPENCODE.JSON` or a read dir spelled in another case stays editable, and its `apply_patch` tool (models named `gpt-…`) checks only a moved file's source.

`yolo` asks for nothing: no permission prompt, approval or sandbox; the container is the boundary ([Docker host](docker-host.md#permissions)). Claude refuses it as root; the container's clients run as `agent`. `danger-full-access` ignores writable roots and team dirs; a Docker container cannot run Codex's sandbox ([Docker sessions](docker-codex.md#docker-sessions)). For `workspace-write` outside the Docker Host:

```toml
[session.clients.codex.modes]
auto = ["-s", "workspace-write", "-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="auto_review"', "-c", "sandbox_workspace_write.network_access=false", "-c", "sandbox_workspace_write.writable_roots=[]"]
```

## OpenCode sessions

OpenCode sessions run on every Host. On the tmux and pty Hosts both modes run `[clients.opencode] executable` (default `opencode`) and refuse to start when its `--version` is not 1.x: where PATH finds a 2.x (Homebrew's `opencode` is 2.x), set it to a 1.x install's absolute path in `~/.agent-pm/config.local.toml`'s `[session.clients.opencode]`. On the Docker Host they run the image's `/opt/agent-pm/bin/opencode` instead (`executable` is ignored; the broker checks its version), in yolo mode, with Ollama at `host.docker.internal`. The TUI loads `session/src/agent_pm/session/clients/opencode/plugin.js` (on the Docker Host, the image's copy), which turns OpenCode's events into `agent-pm-session hook` calls: `session.busy` and `tool` → `working`, `permission.asked` → `blocked`, `session.idle` → `done` with the turn's last assistant text, `session.error` → a failed turn. A start's prompt goes in as `--prompt`; a resume (`opencode --session <ses_…>`) types its prompt once the plugin has started. A long message shows as `[Pasted ~N lines]` in the TUI but is sent in full.

OpenCode keeps no jsonl transcript: its sessions are in `opencode.db` (tmux and pty: `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db`; a Docker Host session's: `~/.agent-pm/docker-state/<name>/opencode/opencode/opencode.db`, which a resume needs). `session_status` reads it read-only. To see what a session holds, or whether a `send` reached it:

```bash
python3 -I session/src/agent_pm/session/clients/opencode/transcript.py <ses_id> [--db <path>] [--limit N]
```

It prints the session's newest `N` (default 100) user and assistant texts, oldest first, one JSON line each: `{"time": "2026-10-18T12:00:00.123Z", "role": "user", "text": "…"}`. A missing db or unknown session: exit 1 and one `opencode transcript: …` line on stderr. The `ses_…` id is the last `client_session` line's `client_id` in the run's `<workdir>/run/run.jsonl`.
