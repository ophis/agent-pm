# Sessions and client policy

How the sessions agent-pm starts are set up, how to list and show them, and the flags role runs pass their client.

## Session settings

Every session agent-pm starts, agent runs and workers, loads your user settings with every plugin except `agent-pm` (`[clients.<client>] flags` in `session/src/agent_pm/session/config.toml`): only a client you start, the commander, has `/agent-pm:manage`. Each such session's client also carries `AGENT_PM_AGENT_SESSION=1`, under which `agent-pm-crew` `run`/`worker`/`reopen`/`issue`/`role`/`hold`/`resume`, `agent-pm-session` `start`/`restart`/`resume`/`client-resume`, `agent-pm-delegate` `run`/`resume` and `agent-pm-linear` `router`/`resume` exit 2 (their forms that start nothing, such as `--dry-run`, `--list` and `--brake`, still run), even run by a checkout's `bin/` path: a guard against slips, not isolation (`env -u` clears it). Claude agent runs load no MCP servers (`--strict-mcp-config` in `[policy.claude]`, `delegate/src/agent_pm/delegate/config.toml`); Codex ones load those your Codex config names. A trusted cwd adds its project settings (`trusted_dirs` in `delegate/src/agent_pm/delegate/config.toml`).

Codex sessions also start without its built-in browser, computer use, Apps and plugin suggestions, so a run cannot open a web page or a third-party service to act outside its outcome; web search, image generation, subagents and your own plugins stay. Checked against Codex 0.163.0, whose plugin ids can change with a release:

| Tool | Packaged `[clients.codex] flags` entry |
|---|---|
| Browser and Chrome (MCP server `node_repl`) | `-c plugins.<id>@openai-bundled.enabled=false`, `<id>` each of `browser`, `chrome`, `chrome-dev`, `chrome-internal` |
| Computer use (`node_repl`, `cua_repl`) | the same, `<id>` each of `computer-use`, `unified-computer-use` |
| Apps (`codex_apps` connectors, the browser connector among them) | `--disable apps` |
| Plugin suggestions, which can install a plugin | `--disable tool_suggest` |

The feature flags `browser_use`, `computer_use` and `in_app_browser` do not turn these off, and `--disable plugins` would turn off your plugins too. To get a tool back, drop its entry from a `[session.clients.codex] flags` list in `~/.agent-pm/config.local.toml`: a local list replaces the packaged one whole, so copy the rest. Every launch reads this list, a resume and `client-resume` included, so a change reaches sessions started before it. A Codex resume keeps the run's own flags (`[policy.codex]` in `delegate/src/agent_pm/delegate/config.toml`) from its start, where Claude's takes the current ones: Codex records a session's writable grants at its start and a resume never widens them. For the same reason the list refuses `--add-dir`.

In the commander's Claude Code session the plugin's mod (`manager/hooks/quiet.tsx`) always hides Monitor tool-call rows and notices (each event, the expiry, the stream's end), plus Bash tool-call rows whose command contains `agent-pm-crew next-event` or `agent-pm-crew ack`. Other tools, Bash commands and notices draw as usual; the model's content is unchanged. Ctrl+o shows Monitor notices; the hidden tool-call rows stay hidden.

On Claude Code's and Codex's SessionStart `startup`, `resume`, `compact` (automatic or `/compact`) and `clear`, not `fork`, the plugin's hook (`manager/src/agent_pm/manager/rearm.py`) gives a commander under `agent-pm-crew hold` a re-arm prompt: stop its own listener, `attach`, read the backlog with `pending --content`, ack what it handled, then `attach` again. Claude Code (`manager/hooks/hooks.json`) arms Monitor with that `monitor:` line. Codex (`manager/hooks/manual.json`, declared in `.codex-plugin/plugin.json`) uses Manual mode: run `agent-pm-crew pending --content` at the beginning of every active turn before status reports or new launches; there are no idle notifications. Only a background tool whose runtime contract guarantees notifications to this conversation may run the second `attach`'s `next-event:` command, without `--follow`; after handling and acking each event, run `attach` again and use its newly printed command. A commander session is one whose environment has `AGENT_PM_MANAGER` (a manager name) and `AGENT_PM_HOLDER_ID` and no `AGENT_PM_AGENT_SESSION`; every other session gets nothing, and so does a commander not under `hold` that gives `--manager` to each command. The hook only prints: it reads no Roster, takes no lease, acks nothing and starts no monitor.

After installing or updating the Codex plugin, open `/hooks`, review and trust the agent-pm SessionStart command before relying on this prompt; installation alone does not trust plugin hooks. A changed hook definition needs review and trust again. [Codex plugin hooks](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks) describes this trust requirement.

A prompt over 100,000 bytes (measured JSON-encoded) never goes on the client's command line, as Linux's exec refuses any one argument over 128 KiB. Every client, TUI or headless, gets a one-line pointer to a 0600 file holding it and reads that file itself: a TUI session's under `~/.agent-pm/tui/<name>/prompt/`, a headless run's under its workdir's `prompt/`, on the Docker Host `/run/agent-pm/prompt/prompt.md` (the image's directory; for Codex a 1 MiB tmpfs). The Docker Host refuses any argument over 128 KiB before it starts a container.

## agent-pm-session

`bin/agent-pm-session --help` lists its subcommands. `ls` prints a header, then one tab-separated row per session: `name`, `state` (`working`, `done`, `blocked`, `dead`, `unknown` when its host could not tell), `client`, `sid`, `display`, with `-` for an empty value.

Sessions run on `[session.hosts] default`: `tmux`, or `pty` (each on a pty its own supervisor holds, state in `~/.agent-pm/pty/<name>/`). A `pty` session cannot be shown: `show`, `hide`, `attach-command`, `read` and `tile` exit 1, and its `status` `attach` is null. `[delegate.delegates] harness_host = "pty"` puts only detached runs' harnesses there.

`start --display none` starts a worker without a pane, still opened by the caller, and hidden. Hidden (from that, `hide`, or closing its pane by hand; not from losing the manager's window or detaching its viewer) lasts through `restart` and `resume` until `show`.

Text sent to a Codex tui session mid-turn joins that turn (a steer). When the session has an events file, the first answer after it appends `HH:MM:SS <name> reply <id>` before the turn's `done`, its text kept 30 days in `<events>.replies/<id>`; `agent-pm-crew pending --content` and `next-event --content` print that text. A watcher the steer's hook starts reads the session file within a few seconds, and the turn's Stop hook reads it once more. An answer Codex gives only in the next turn has no `reply`.

## Client policy

Role runs' client flags are `[policy.<client>]` in `delegate/src/agent_pm/delegate/config.toml` (`[policy.claude]`, `[policy.codex]`; none packaged for OpenCode, so none unless a local `[delegate.policy.opencode]` sets them), after the session's `[clients.<client>] flags` and permission mode. Their system-prompt flag (`--append-system-prompt`, Codex's `developer_instructions`) tells a role run its final reply is the outcome block alone; OpenCode has no such flag, its config being the session's. A local list replaces the default whole: copy that flag into it.

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

OpenCode sessions run on every Host, but outside an `isolated` Host (tmux, pty, or `[hosts.docker] isolated = false`) only in a trusted cwd (`Access.project`; a role run's: under `trusted_dirs`, so given by `--cwd`, as its default cwd, the workdir's `agent/`, never is): OpenCode 1.x has no project trust, so in any cwd it runs `.opencode/plugins/*.js` and installs the npm plugins `opencode.json` lists, and `OPENCODE_DISABLE_PROJECT_CONFIG` does not stop it. A start, TUI or headless, and a resume in an untrusted cwd there fail before OpenCode starts: run it on the Docker Host or in a trusted cwd. On the tmux and pty Hosts both modes run `[clients.opencode] executable` (default `opencode`) and refuse to start when its `--version` is not 1.x: where PATH finds a 2.x (Homebrew's `opencode` is 2.x), set it to a 1.x install's absolute path in `~/.agent-pm/config.local.toml`'s `[session.clients.opencode]`. On the Docker Host they run the image's `/opt/agent-pm/bin/opencode` instead (`executable` is ignored; the broker checks its version), in yolo mode, with Ollama at `host.docker.internal`. The TUI loads `session/src/agent_pm/session/clients/opencode/plugin.js` (on the Docker Host, the image's copy), which turns OpenCode's events into `agent-pm-session hook` calls: `session.busy`, `tool`, `permission.replied`, `question.replied` and `question.rejected` → `working`, `permission.asked` and `question.asked` (the question tool's prompt; its reason `question: <header>`) → `blocked`, `session.idle` → `done` with the turn's last assistant text, `session.error` → a failed turn. A start's prompt goes in as `--prompt` (a long one: as in [Session settings](#session-settings)); a resume (`opencode --session <ses_…>`) types its prompt once the plugin has started. A long message shows as `[Pasted ~N lines]` in the TUI but is sent in full.

Each launch's OpenCode config (`OPENCODE_CONFIG_CONTENT`) enables only the `ollama` provider and the `[clients.opencode.providers.<name>]` ones (`enabled_providers`): OpenCode's built-in providers, OpenCode Zen's included, stay off, so `opencode models` under that config lists only these. Every launch passes `--model`: its tier's (`[clients.opencode.tiers]`), else `[clients.opencode] model` (default `ollama/qwen3.6:35b-mlx`), never OpenCode's own pick. Each of these names `ollama/<model>` or `<name>/<model>` of a providers table, else the client refuses its config. A provider is any OpenAI-compatible server, such as one on your LAN:

```toml
# ~/.agent-pm/config.local.toml
[session.clients.opencode.providers.strata]
base_url = "http://<LAN host>:8080/v1"
models = ["qwen3.8-flash-next-iq2_xs"]   # beside the models tiers and model map to it
context = 204800                          # its context window in tokens; default 65536

[session.clients.opencode.tiers]
1 = "strata/qwen3.8-flash-next-iq2_xs"
```

Its API key, on the tmux and pty Hosts, stays OpenCode's: run `opencode auth login` once, pick Other and enter the provider's name (`strata`), and OpenCode keeps the key in its `auth.json`; agent-pm never reads or passes it. On the Docker Host the broker keeps it: `agent-pm-broker login opencode strata` once, for every configured provider ([Docker host › OpenCode](docker-host.md#opencode)). A name is up to 64 letters, digits, `.`, `_` or `-`.

Each model's config entry carries its server's context window (`limit`): a providers table's `context`, `[clients.opencode] ollama_context` for `ollama/` models (both default 65536, Ollama's minimum in [Docker host › OpenCode](docker-host.md#opencode)). OpenCode compacts a session once a step reaches that window less a headroom, at most half the window: the compaction's reply (each request's output cap, OpenCode's 32000 or a quarter of the window if smaller) and 20000 for its summary prompt and the step that crossed the line. Without it OpenCode knows no window for these providers and compacts only after the server refuses a full context, when the compaction request itself no longer fits. Set each to the server's real window (Ollama's `OLLAMA_CONTEXT_LENGTH`): a smaller value only compacts early, a larger one lets a request overflow.

A crew worker's cwd is always trusted (you pick it), so `agent-pm-crew worker NAME --client opencode --cwd DIR` starts on tmux in any `DIR`; a role run only under `trusted_dirs`; a board run never (its cwd is the workdir's `agent/`): run OpenCode board runs on the Docker Host ([Linear board › OpenCode](board.md#opencode)).

On the tmux and pty Hosts OpenCode needs the host's Ollama for `ollama/` models ([Docker host › OpenCode](docker-host.md#opencode) steps 1–3) and an OpenCode 1.x. With none on PATH, `npm install -g opencode-ai@1.19.3`; ignore npm's warning that its postinstall did not run. Beside a 2.x, install 1.x under its own prefix, leaving the 2.x as it is, and point `executable` at it:

```bash
npm install -g --prefix ~/.local/opencode-1 opencode-ai@1.19.3
~/.local/opencode-1/bin/opencode --version   # 1.19.3
```

```toml
# ~/.agent-pm/config.local.toml
[session.clients.opencode]
executable = "/Users/<you>/.local/opencode-1/bin/opencode"   # absolute: ~ is not expanded
```

OpenCode keeps no jsonl transcript: its sessions are in `opencode.db` (tmux and pty: `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db`; a Docker Host session's: `~/.agent-pm/docker-state/<name>/opencode/opencode/opencode.db`, which a resume needs). `session_status` reads it read-only. To see what a session holds, or whether a `send` reached it:

```bash
python3 -I session/src/agent_pm/session/clients/opencode/transcript.py <ses_id> [--db <path>] [--limit N]
```

It prints the session's newest `N` (default 100) user and assistant texts, oldest first, one JSON line each: `{"time": "2026-10-18T12:00:00.123Z", "role": "user", "text": "…"}`. A missing db or unknown session: exit 1 and one `opencode transcript: …` line on stderr. The `ses_…` id is the last `client_session` line's `client_id` in the run's `<workdir>/run/run.jsonl`.
