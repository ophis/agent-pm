# Docker host

`agent-pm-broker` builds the worker image and starts and closes worker containers on macOS (Apple silicon) with
Docker Desktop. It keeps the logins and the GitHub App key in the login keychain and writes credentials only into a
container's tmpfs. Session's Docker Host calls it ([Run workers](#run-workers)); the sections up to [Close](#close)
install the broker by hand; checks for each step: [Docker checks](docker-checks.md). JSON interface: [Docker broker
protocol](docker-broker-protocol.md); Codex: [Codex operations](docker-codex.md); each credential's readers, lifetime
and revocation: [Credentials](credentials.md).

The host needs no Node and no personal Codex: the clients run in the image. On subscriptions a container holds a
model credential; an API key behind a host model relay, so that containers hold none, is not supported.

Run every step as yourself, `HOME` your account home. `B` is `/usr/local/libexec/agent-pm/agent-pm-broker`; `PKG`
the installed agent-pm plugin directory.

## Install

Needs Docker Desktop (running) and `gh` (logged in).

`install.sh` installs Docker Desktop with the `docker-desktop` cask, stops unless its binary's CPU types include
`uname -m` and its `LSMinimumSystemVersion` is at most `sw_vers`, and starts it once without its windows: its own
`Contents/MacOS/install --accept-license --user=<you>` (with the `sudo` password it holds), then
`UseVirtualizationFrameworkRosetta: false` and `DisplayedOnboarding: true` in
`~/Library/Group Containers/group.com.docker/settings-store.json` (settings already there are kept).

**Rosetta** stays off and `install.sh` never installs it: the worker image is arm64. For an amd64-only base image in
your Dockerfile, run `softwareupdate --install-rosetta`, then turn on Docker Desktop › Settings › General › **Use Rosetta
for x86_64/amd64 emulation on Apple Silicon**. Rosetta cannot be uninstalled, so `scripts/install-cleanup.sh` cannot
undo it.

```sh
sh "$PKG/broker/install.sh"
```

It downloads the broker asset of the plugin's version, installs it root-owned (`sudo` asks once) only after
`gh attestation verify` proves the agent-pm release workflow built it from that version's tag, and seeds
`~/.agent-pm/docker/worker/Dockerfile` (your image, edit freely) only if `~/.agent-pm/docker/worker` does not exist.
The asset appears a few minutes after a Release is published; until then it exits non-zero: retry. `--build`
instead builds and ad-hoc signs it locally with the hardened runtime (needs Go, `brew install go`, and the Xcode
command line tools).

To upgrade, rerun it (your Dockerfile is never overwritten), then [log in](#log-in) again. Claude Code upgrades with
the image, Codex by its [version policy](docker-codex.md#container-versions); the image's `gh` is the version agent-pm
pins (`broker/internal/launcher/assets/Dockerfile`), whatever your Dockerfile installs. So is its OpenCode:
`assets/opencode-install.py` installs it from npm tarballs checked against recorded sha512 hashes, and an OpenCode
start refuses an image below the broker's `opencodeMinVersion` (`broker/internal/launcher/versions.go`): rebuild.

Checks: [Docker checks › Install](docker-checks.md#install).

## Log in

After every install or reinstall, run all three logins: `login claude`, `login github agent-pm-bot` and
`login codex` ([Codex operations](docker-codex.md#commands)), plus `login claude <name>` for each other registered
[Claude account](#claude-accounts) and `login opencode <name>` for each configured OpenCode provider. The keychain
trusts the broker by its code requirement (an ad hoc signature's cdhash), not its path, so a reinstalled broker cannot read the old one's items without asking;
`start` never asks, failing instead (`login_required`, or `keychain_failed` for an old GitHub item). Each login asks its terminal questions first, without
echo:

| Login | Terminal | macOS dialog, at most | Click |
|---|---|---|---|
| `login claude [<name>]` | the token; the login keychain password only when the item's access needs a change | one change-access dialog, after the password; one read dialog, announced on the terminal first | Allow; Always Allow |
| `login github agent-pm-bot` | the login keychain password, when a step is not yet in place | one change-access dialog (Deny / Allow), after the password | Allow |
| `login codex` | the login keychain password, when a step is not yet in place, before the browser opens | one change-access dialog (Deny / Allow), after the password | Allow |
| `login opencode <name>` | the provider's API key; the login keychain password only when the item's access needs a change | at most one change-access dialog (Deny / Allow), after the password | Allow |
| `start` | none | none: a token or key it cannot read without one fails with `login_required`, naming unlocking the login keychain or the login to run | |

What each login hands a container: [Credentials › Who logs in](credentials.md#who-logs-in).

**Claude**:

```sh
claude setup-token          # copy the token it prints
"$B" login claude           # paste it; nothing echoes
"$B" login claude work      # another registered account's token, from its own subscription
```

It saves the token in the account's item, `agent-pm-claude` / `<name>` (`default` without a name), replacing an
earlier one, makes its access trust only the broker as `login github` does (prompt `Login keychain password, to make
Claude trust only the broker (hidden):`), reads it back and prints `Claude account <name> setup-token saved; keychain
trusts only the broker`. If macOS would still ask, it first prints `macOS will ask to let agent-pm-broker use
agent-pm-claude <name>: enter the login keychain password if asked and click Always Allow`, then reads once with the
dialog and once without, proving Always Allow was clicked.

Checks: [Docker checks › Claude login](docker-checks.md#claude-login).

**GitHub**: the private key of your one GitHub App ([create it](#create-the-github-app)), whatever its name, must be
a generic-password item in the login keychain, service `agent-pm-bot`, account its App ID, content the base64 key. Store it so (a bare `-w` prompts and
truncates the key at 128 characters):

```sh
security add-generic-password -s agent-pm-bot -a <App ID> -U -w "$(base64 -i <key>.pem)" ~/Library/Keychains/login.keychain-db
"$B" login github agent-pm-bot
```

The login restricts the item's access in place (content and account kept): partition list to only the broker's
partition ID, then read/export entries to only the broker (a wrong password: `keychain status -25293`). It reads the
access back and checks the key is not empty before printing success; else it exits non-zero saying what is done
and what is not: rerun it. Then it asks GitHub which App the key is (`GET /app`, its slug), looks up `<slug>[bot]`
on GitHub's public user API (no credential; needs network) and keeps both in the broker-only item
`agent-pm-github-users`, for the containers' git identity; it prints
`GitHub App <slug> bot user <slug>[bot] has ID <ID>`. Set `[session.hosts.docker.github] app = "<slug>"` in
`~/.agent-pm/config.local.toml` (`install.sh` writes it): a start naming another App is `manifest_rejected`. The App must be private (installable only on your account): every session gets a token for every repo of
every installation.

Checks: [Docker checks › GitHub login](docker-checks.md#github-login).

**OpenCode**: for each `[session.clients.opencode.providers.<name>]` table (Ollama needs none):

```sh
"$B" login opencode strata   # paste strata's API key; nothing echoes
```

It saves the key in a new broker-only item `agent-pm-opencode` / `<name>`, replacing an earlier one, makes it trust
only the broker (prompt `Login keychain password, to make the <name> API key trust only the broker (hidden):`) and
reads it back. A Docker OpenCode start needs every configured provider's key: a missing one fails with
`login_required`, naming this command.

### Create the GitHub App

`install.sh` does not create it. In GitHub › Settings › Developer settings › GitHub Apps › New GitHub App:

1. Owned by you, installable **Only on this account** (private).
2. Webhook: off (clear **Active**).
3. Repository permissions: Contents and Pull requests **Read and write**; Checks, Actions and Metadata **Read-only**.
4. Create it, note its **App ID**, then **Generate a private key** (a `.pem` download).
5. **Install App** on only the repositories agents may push to.

Then store the key and run `login github` as [above](#log-in); `install.sh` asks for the `.pem` and the App ID, does
both and writes the App's slug to `github.app`.

## Build

```sh
"$B" build --agent-pm "$PKG"              # Docker's progress on stderr, then {"image":"agent-pm-worker:<key>","digest":"sha256:…","rebuilt":true}
"$B" build --agent-pm "$PKG"              # the same, "rebuilt":false
"$B" build --agent-pm "$PKG" --max-age 0  # rebuild now
```

A changed Dockerfile or agent-pm version gives a new key. No build log shows a secret. Rebuilds skip Docker's cache,
so they install npm's latest Claude Code. `--max-age N` rebuilds an image N or more seconds old under the same key (a
start passes 604800, 7 days); if that rebuild fails (e.g. offline) it keeps the image, printing `agent-pm-broker: kept
<image>: refreshing it failed: <reason>`, except `--max-age 0`, which fails.

After a rebuild, `build` removes other keys' `agent-pm-user:<key>` and `agent-pm-worker:<key>` images not leased in
the last hour, and untagged ones a same-key rebuild left, keeping any image a container (running or stopped) came
from; it prints `agent-pm-broker: removed <image>` or `kept <image>: <reason>` for each. Every `build` leases its key
by touching `~/.agent-pm/docker/images/<key>` (at start, before the second stage, at the end of a rebuild), so
concurrent builds of two packages (two worktrees, a worker's check beside a board run) keep each other's images.
`~/.agent-pm/docker/images.lock` separates removal from leasing: a build that cannot take it fails with
`build_failed`; a removal pass removes nothing, printing `agent-pm-broker: kept older images: cannot take the image
lock: <reason>`.

Checks: [Docker checks › Build](docker-checks.md#build).

### Plugins

A container's Claude Code and Codex load the plugins its image holds (OpenCode loads none): `[session.hosts.docker] plugins`, default `["autopilot"]`
(the team's `/autopilot:build`, `/autopilot:light-build` and `/autopilot:review`); `[]`: none. Never agent-pm's own:
every session runs with it disabled, and the image holds the team at `/opt/agent-pm/team`.

```toml
[session.hosts.docker]
plugins = ["autopilot", "other@its-marketplace"]
```

Each name is a plugin installed in Claude Code at user scope (`claude plugin install`), `name` or `name@marketplace`
when two marketplaces have one. `build` passes each as `--plugin NAME`: the broker finds it in
`~/.claude/plugins/installed_plugins.json` and copies the version Claude Code loads now (its `installPath`) to
`/opt/agent-pm/plugins/<name>`, root-owned and read-only for `agent`, listed in `CLAUDE_CODE_PLUGIN_DIRS` (a launch
env's own replaces it), so it loads as `<name>@inline` with its `/<name>:` commands. The copies are part of the image
key: a whitelist change or `claude plugin update` rebuilds at the next start (Claude Code's `.in_use/` and
`.orphaned_at` markers are not copied). Codex uses the same payload ([Native plugins](docker-codex.md#native-plugins)).
One whitelist per account: another is another key, whose rebuild removes this one's images.

The build fails before Docker with `build_failed` naming the plugin (`Plugin <name> is not installed: no user-scope
entry in <record>.` when not installed) if a name matches two marketplaces, resolves outside the plugin cache, has no
valid `.claude-plugin/plugin.json` name, or holds a symlink leaving its directory or dangling; a start fails in
preflight the same way. Whitelisting trusts the plugin's code (Claude loads its hooks, MCP servers and scripts) beside
the container's credentials.

Checks: [Docker checks › Plugins](docker-checks.md#plugins).

## Start

```sh
mkdir -p ~/src/demo ~/.agent-pm/docker-state/worker_1/claude
cat > /tmp/worker_1.json <<EOF
{"name": "worker_1", "client": "claude", "generation": "$(openssl rand -hex 16)", "image": "$I",
 "cwd": "$HOME/src/demo", "workspace": "agent-pm-ws-worker_1",
 "claude_dir": "$HOME/.agent-pm/docker-state/worker_1/claude", "github": null, "labels": {}}
EOF
"$B" start --manifest /tmp/worker_1.json
```

It prints `container` (`C` below), `endpoint`, `image_digest` and the actual `clients` versions; Codex starts add the
[version audit](docker-broker-protocol.md#success-fields).

Checks: [Docker checks › Start](docker-checks.md#start).

## What a container gets

Each row's source holds the exact values.

| What | Source | Summary |
|---|---|---|
| `docker run` flags | `broker/internal/launcher/start.go` `create` | user `agent`, all capabilities dropped, `no-new-privileges`, bridge network, `tini` as PID 1, the `agent-pm.session`, `agent-pm.generation` and `agent-pm.client` labels |
| `docker run` env | `create` | the client home (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, or OpenCode's `XDG_DATA_HOME` with its self-update and LSP downloads off); npm, uv and pip caches under `<cwd>/.agent-pm-cache` |
| Mounts | `create`, `mounts.go` | the [workspace volume](#workspace-volumes) at cwd; Claude and OpenCode: its home bound at its host path; Codex: tmpfs home and runtime, its `sessions` bound |
| Secrets tmpfs | `create`; written by `broker/internal/broker/keychain.go` `Credentials.Inject`, `github.go` `Issue`/`Refresh`, `codex.go` `Codex.Inject`, `opencode.go` `OpencodeKeys.Inject` | `/run/agent-pm/secrets`, agent-owned, `noexec`: the client credential (OpenCode: each configured provider's key, `opencode/<name>.key`) and one GitHub token per installation ([GitHub tokens](docker-broker-protocol.md#github-tokens)); read only through `with-secrets` and the `gh`/git credential wrappers |
| Start-time files | `create` (`bootstrap.py`, `hook-stub`) | client state that skips onboarding and trusts cwd; the hook stub |
| Exec env | `session/src/agent_pm/session/hosts/docker/host.py` `_exec_env` | the client's launch env without `PATH`; with `[session.hosts.docker.github] app`: `AGENT_PM_PR_AUTHOR` and the git author and committer `<app>[bot] <<bot user ID>+<app>[bot]@users.noreply.github.com>`, the ID from start's `github_user_id` |
| Image: base | `~/.agent-pm/docker/worker/Dockerfile` (seeded from `broker/worker/Dockerfile`) | your image: tools such as tmux, git, gh, Node, uv and Claude Code |
| Image: agent-pm layer | `broker/internal/launcher/assets/Dockerfile` | agent-pm's session, delegate and utils sources and venv, the [plugins](#plugins), Codex, OpenCode, ripgrep, the `gh` and git credential wrappers (`/etc/gitconfig`), `core.hooksPath = .githooks` (`/etc/gitconfig`: each checkout runs its own branch's `.githooks/`, none without one), `with-secrets`, `relay`, `/opt/agent-pm/bin` first on `PATH`; no git identity |

## Refusals

What the broker refuses before creating any container, with each exit code: [Docker checks › Refusals](docker-checks.md#refusals).

## Close

```sh
"$B" close --container "$C"   # {"container":"…","removed":true}
"$B" close --container "$C"   # removed:false: already gone
```

`docker ps -a --filter name="$C"` lists nothing.

## Audit log

Every `start`, `refresh` and `close` appends one JSON line to `~/Library/Logs/agent-pm/broker.jsonl` (directory 0700,
file 0600, written as you) recording each credential it issued, renewed, withdrew or revoked, never a token or key. A
backlog retry (each `start`, `close` and `status`, and a `refresh` that queues a token) that revokes or drops another
container's token adds a line for that container. Past 10 MiB it moves to `broker.jsonl.1`, replacing the previous
one. Writing is best effort: an unwritable line is dropped, the result unchanged. No container mounts it. Source:
`broker/internal/audit/`. Query: `jq 'select(.session=="NAME")' ~/Library/Logs/agent-pm/broker.jsonl`.

| Field | Value |
|---|---|
| `time` | RFC 3339, UTC |
| `command` | the broker command |
| `event` | `start`, `refresh`, `close`, or `revocation-retry` (another container's backlog tokens) |
| `session`, `generation`, `container` | from `agent-pm-<session>-<generation>`; null when start could not read its manifest |
| `image`, `image_digest`, `mounts` | start only: the manifest's tag, the image ID, each `--mount`/`--tmpfs` as `{type, source, target, mode}`; null for a step not reached |
| `credentials` | one entry per credential, below |
| `error` | the error `code` ([Docker broker protocol](docker-broker-protocol.md)); null on success |

| Entry field | Value |
|---|---|
| `kind` | `claude-setup-token`, `codex-access-token` or `github-installation-token` |
| `result` | `issued`, `renewed`, `withdrawn` (deleted with its container), `revoked`, `pending` (in the backlog; a later `revocation-retry` line says `revoked`, or `expired-unrevoked` if it expired first) |
| `reason` | `pending` only: why not revoked yet |
| `expires_at` | RFC 3339; null when unknown (the setup-token, a withdrawn Codex token) |
| `account`, `installation` | GitHub: the installation's account and ID (no ID on retry lines) |
| `repository_selection`, `repositories` | GitHub `issued`/`renewed`: `all` or `selected`, and every repo the token reaches (all pages of `GET /installation/repositories`); both null with `repositories_error` if that listing failed (the token is still issued) |

## Run workers

Make the Docker Host the default in `~/.agent-pm/config.local.toml`:

```toml
[session.hosts]
default = "docker"
```

| `[session.hosts.docker]` key | Default | Meaning |
|---|---|---|
| `docker` | `/usr/local/bin/docker` | the docker CLI, an absolute path |
| `plugins` | `["autopilot"]` | the plugin payloads every image holds ([Plugins](#plugins)); `[]`: none |
| `github.app` | `""` | the slug of the GitHub App `login github` saved, whose installation tokens every session gets, as `AGENT_PM_PR_AUTHOR=<app>[bot]` names it; `""`: none |
| `isolated` | `true` | its sessions' clients run in yolo mode ([Permissions](#permissions)); `false`: auto mode, as on tmux |

Then `agent-pm-crew worker NAME --client claude` starts NAME in its own container; the other `agent-pm-crew` verbs,
the Viewer, `--display none`, `[session.viewer]` and `ls --descendants` work as on tmux. Every docker call goes to
`unix://<account home>/.docker/run/docker.sock`, whatever `DOCKER_HOST`, `DOCKER_CONTEXT` or `DOCKER_CONFIG` say.

Text a client copies in a container's pane (Claude Code's mouse selection, for one) reaches your clipboard by OSC 52:
the container's tmux sets `set-clipboard on` and `allow-passthrough on`. Your terminal must support OSC 52 (Ghostty
does), and a host tmux showing the pane needs `set -g set-clipboard on` in `~/.tmux.conf`.

A session's GitHub tokens are renewed 10 minutes before the first expires, by the TUI session's host run supervisor or
the headless run's harness; a failure writes `github_refresh_failed <reason>` to the session's events or the harness's
log, and retries every minute.

### Permissions

The container is the isolation boundary, so every Docker session (crew worker, role or headless run; start, restart
and resume) runs its client in yolo mode: no permission prompt or approval; it writes files, runs `gh` and reaches the
network unasked, within the container's reach (workspace volume, the session's GitHub tokens, the network). Flags:
[Client policy](sessions.md#client-policy).

### Role runs

Role runs (`agent-pm-crew role`, attended board runs) run their client in a container; their harness stays on the
host, on another Host:

```toml
[delegate.delegates]
harness_host = "tmux"
```

A headless agent (an unattended board run, `agent-pm-delegate run` without `--detach`) runs its client by
`docker exec -i` in its own container, with no tmux, hook relay or supervisor, its output streamed to the harness;
`ls`, `attach` and the Viewer do not show it, and a TUI start of its name is refused while it runs. Its container
closes when the client exits or is stopped, or, if the harness died, at the next Docker start or close. A run's
volume is `agent-pm-ws-<agent name>` at `<workdir>/agent`, kept across a board issue's revision rounds;
`agent-pm-crew reopen` resumes it on the Docker Host with the same history and volume, whichever mode started it.

On the Docker Host the Delegate:

- refuses, before any container and naming the key, a `harness_host` that is empty or the Docker Host, a `read` dir
  other than the image's team (`{{team}}`, a core role's `{{role_team}}`, the tasks dir), any `write` dir, a role
  `cwd`, a cwd other than the agent dir (a `trusted_dirs` project cwd included), a `team_dirs` role, and an input
  whose `Reference:` block names two `Repo:` lines or one off github.com;
- gives the agent the image's team at `/opt/agent-pm/team` and its `python3`;
- writes that `Repo:` line as `owner/name`, a local clone's read from its `origin`;
- sets `AGENT_PM_USERS`: the configured `users` without any `[bot]`;
- asks a `local` or `orchestrator` run for the deliverable inline, never `deliverable_file` (still saved to `out`),
  and a run that publishes its own deliverable for none;
- drops the outcome's `files`, noting `files_dropped` in `run.jsonl`, so an engineer run posts no spec or plan to
  Linear.

Before the first role run, [log in](#log-in) to GitHub and install the App on every repo the runs need, `private_docs`
included: every Docker session can read and write exactly those private repos.

In the container `repo.py worktree` makes a fresh blobless clone at `<Workdir>/src/<owner>/<name>-<slug>`, never a
`[local_clones]` worktree; `repo.py` treats `AGENT_PM_PR_AUTHOR` as the PR author (its docstring:
`delegate/src/agent_pm/delegate/team/tools/repo.py`). An engineer opens its draft PR through the `gh` wrapper as `<app>[bot]`; the Delegate's ready gate on the host
marks it ready, you approve with your own account, and revision rounds push to the same branch. Then
[review before pulling](#review-before-pulling).

### Claude accounts

A Docker Claude session runs on one Claude account, a subscription whose setup-token the broker keeps: `default`
unless a worker or role selects another. Register each other one in `~/.agent-pm/config.local.toml`, then
[log it in](#log-in) (`"$B" login claude work`):

```toml
[session.clients.claude.accounts.work]
dir = "~/.claude-work"   # its CLAUDE_CONFIG_DIR on the host, absolute after ~; absent → ~/.claude
account = "me@work"      # a display string, e.g. its login email
```

No other key and no secret. A name is 1–64 lowercase letters, digits, `.`, `_` or `-`, never `setup-token`.
`default` is always registered, its `dir` `~/.claude` (never written; a `[…accounts.default]` table may set only
`account`). The usage probe reads `dir`: `claude -p /usage` on the host with `CLAUDE_CONFIG_DIR=<dir>`, unset for
`default` and any account whose `dir` is `~/.claude`.

With no selection and two or more accounts registered (`default` counts), a Docker Claude start picks one by usage
left (100 minus the higher of its 5h and weekly used percent):

1. the account under both usage-gate thresholds (5h below 90 %, weekly below 100 %) with the most left;
2. none under: the account with the most left;
3. no reading known: none (`default`).

A tie goes to `default`, then name order. Each account's reading is cached in `~/.agent-pm/usage/claude.json` for
5 minutes (a failed probe's for 30 seconds), so a start or gate check within that time probes nothing; each probe prints
`claude usage: account <name>: five_hour=<p>% weekly=<p>%` (or `unknown`) to stderr, and the start
`claude account <name>: picked (…)`. Resumes pick again.

Select one:

- **Worker**: `agent-pm-crew worker NAME --client claude --account work` (`agent-pm-session start NAME --client claude
  --account work`); restart and resume keep it.
- **Role**: keyed by client, so a run whose client an Agent label switches to Codex or OpenCode gets none:

  ```toml
  [delegate.roles.engineer.accounts]
  claude = "work"
  ```

  `[delegate.accounts]` holds every role's, and for board runs `[orchestrator.core.accounts]` and
  `[orchestrator.core.roles.<role>.accounts]` come after; a later table wins entry by entry, `""` unsets one.
  A Delegate resume, a board run's included, resolves it again from the current config; `agent-pm-crew reopen`
  restarts on the account its run record holds.

The session takes the selection as `AGENT_PM_ACCOUNT` ([agent-pm-session](sessions.md#agent-pm-session)) and writes
it into the manifest as `claude_account`; the broker injects that account's token alone. Each failure comes before
any container:

| Cause | Result |
|---|---|
| an account (`--account` or a role entry) not registered | exit 2, `account 'nope': not a registered account of client claude ([clients.claude.accounts.nope])` |
| an account for `--client codex` or `opencode` | exit 2, `client codex has no accounts: remove account 'work'` |
| `--env AGENT_PM_ACCOUNT=…`, or `AGENT_PM_ACCOUNT` in a `[delegate.policy.<client>] env` | exit 2, `AGENT_PM_ACCOUNT is set by --account or a role's accounts table` |
| a registered account not logged in | `login_required`, `No Claude setup-token for account work: run agent-pm-broker login claude work.` |

- Config picks the account; it is no access control: any process running as you can start a container with any
  logged-in account ([Credentials › Risks](credentials.md#risks)).
- With two or more accounts on the Docker Host, the usage gate and `--brake` read the account a start with no
  selection picks, so the gate skips Claude runs only when every account is over a threshold (`usage-skip`, reason
  `usage`) or none has a reading (`usage unknown`). A worker's or role's selected account is gated the same way, not
  by its own usage: that needs an account parameter on the frozen `Session.usage`. With one account, or on tmux, the
  gate probes the host's Claude login (the `[session.clients.claude]` table's). The board's limit hold stays per
  client: one account's usage limit holds every board run on Claude.
- The tmux Host checks a selection as above, then ignores it: the session runs on your own Claude login.

### OpenCode

OpenCode sessions run the host's Ollama models, or a configured provider's ([OpenCode sessions](sessions.md#opencode-sessions)).
Ollama needs no login; each configured provider's API key comes from the broker ([Log in](#log-in)), which writes it
to the container's `/run/agent-pm/secrets/opencode/<name>.key` at start ([Credentials](credentials.md#who-logs-in)).
A provider is reached at its `base_url` from the container's bridge network, which Docker Desktop routes through the
Mac: a LAN address such as `http://10.0.0.5:8080/v1` works as on the Mac, `localhost` is the container itself. The
first worker to reach a LAN address makes macOS ask **Allow Docker to find devices on local networks?**: allow it (later:
System Settings › Privacy & Security › Local Network › Docker), or the provider is unreachable. An
`http://` `base_url` sends the key in plaintext: anyone on the LAN can capture it ([Risks](#risks)). Once, on the Mac:

1. Keep Ollama on `127.0.0.1` (its default; leave `OLLAMA_HOST` unset): the container reaches it through
   `host.docker.internal` ([Risks](#risks)).
2. Set its context length to at least 65536, or tool calls fail: for the Ollama app,
   `launchctl setenv OLLAMA_CONTEXT_LENGTH 65536`, then quit and reopen it; for `ollama serve`, start it with
   `OLLAMA_CONTEXT_LENGTH=65536`. A larger one: set `[session.clients.opencode] ollama_context` to it, or
   OpenCode compacts as for 65536 ([OpenCode sessions](sessions.md#opencode-sessions)).
3. Pull every `ollama/` model of the tiers and `model`, `ollama pull qwen3.6:35b-mlx` by default
   (`[session.clients.opencode.tiers]`; `ollama_port`, default 11434, is its port).

The image's OpenCode is fixed at 1.19.3 ([Install](#install)) and the Client gives its config
(`OPENCODE_CONFIG_CONTENT`: the Ollama provider at `http://host.docker.internal:<ollama_port>/v1`, the configured
providers, each with `apiKey` `{file:/run/agent-pm/secrets/opencode/<name>.key}`, and no other, no auto-update):
none of the Mac's OpenCode install, config or plugins goes into the container (the cwd's own `opencode.json` and
`.opencode/` still load: OpenCode has no project trust). An existing
`~/.agent-pm/docker/worker/Dockerfile` needs no edit for it: the agent-pm layer installs OpenCode on top of any user
image. Its session state is `~/.agent-pm/docker-state/<name>/opencode/` (`opencode_dir`). Then
`agent-pm-crew worker NAME --client opencode` starts a worker in a container; an OpenCode board run:
[Linear board › OpenCode](board.md#opencode). An OpenCode worker's window never gets narrower than 40 columns,
since its Bun crashes at 37 or fewer on Linux: a narrower grid cell or terminal shows the window's left part.

### Lifecycle

Each start, restart or resume runs a new generation in a new container from the current image. `restart` closes the
running container first; `resume` (after `close`, or once the container is gone) starts one in the same cwd. Both
keep the client history and [workspace volume](#workspace-volumes) and continue the conversation; only a new start
clears them. A resume whose conversation file is missing or a symlink fails before any container with
`no native history to resume`. Resume with `agent-pm-session resume <name>`, never `client-resume`, which runs the
client on the host. Claude, [Codex](docker-codex.md#docker-sessions) and [OpenCode](#opencode) terminals and headless runs are supported;
other clients, detached sessions and a start while a container of that name exists fail before creation.

**Preflight**, before every start: client and mode supported; the cwd exists (`Session.preflight` also accepts one
not yet made: a role run makes its agent dir after); the broker's `version` matches (else it points to
[Install](#install)); `build` succeeds, rebuilding an image 7 or more days old, its progress on stderr. `resume` runs it
before touching anything; `restart` closes first, so after a failed preflight `resume` continues the worker once
fixed. Missing client credentials fail at the broker's `start`, before creation, naming the host
`agent-pm-broker login` command to run; the start is not retried.

**Waits**: `agent-pm-crew` waits up to 1920 s for a start or resume (a rebuild alone may take 1800 s), then sends
SIGTERM and, 60 s later, SIGKILL. From just before the broker's `start` until the client runs, a start holds SIGTERM,
SIGINT and SIGHUP back (an ignored one stays ignored); on one it removes its container, then the signal acts. Stopped
later, it leaves its worker running.

**Plan**: just before the broker's `start`, each generation freezes its plan in `<generation>.plan.json` and prints
it to stderr, to check against `docker inspect` (no env values, flags or token; the broker reads only the manifest):

```text
docker plan NAME GENERATION: image agent-pm-worker:<key> sha256:<digest>
docker plan NAME GENERATION: workspace agent-pm-ws-NAME at <cwd> rw
docker plan NAME GENERATION: mounts <claude dir> rw
docker plan NAME GENERATION: credential claude-setup-token
```

**State**, `~/.agent-pm/docker-state/<name>/`, kept by `close`: the client's config dir (cleared at each new
start); per generation the manifest `<generation>.json`, the plan, `<generation>.result.json` (container, image and
digest, client versions, the broker start's begin and end, [start timing](#start-timing) `phases` and `launched_at`),
`<generation>.seq` (last hook event delivered) and `<generation>.stale` (image no longer current); `host.json` (the
host's view, with the events path); a headless run's `pipe.lock` (held by its harness while the client runs); the
hook supervisor's `supervisor.lock` (holding the generation) and `supervisor.pid`; `rejected.jsonl` (`gen`, `seq`,
`reason` per refused event, never its content).

**Claude settings**: each new start writes a `settings.json` with the fixed unattended values of `UNATTENDED` in
`session/src/agent_pm/session/clients/claude/common.py` (no stop at the Workflow usage warning, a full context or a
usage limit; no prompt suggestions; concise; fullscreen), and the host Claude's `model`, `effortLevel`,
`modelSettings` and `env.CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` (from `$CLAUDE_CONFIG_DIR/settings.json`, else
`~/.claude/settings.json`; unreadable: Claude's default model), each `modelSettings` entry without its
`autoCompactWindow` and an entry left empty not written. No other host setting, credential, hook, plugin,
status line or permission reaches it. `--model`/`--effort` and role config still win; a resume keeps its session's
file.

### Start timing

Each start, restart or resume prints where its time went to stderr, three blocks, each once its part succeeded; each
phase counts from the end of the one before:

```text
docker timing NAME: preflight 41.20 s
agent-pm-broker: start preflight 0.01 s
…
agent-pm-broker: start total 38.75 s
docker timing NAME GENERATION: checks 0.12 s
…
```

| Line | Phase | Covers |
|---|---|---|
| `docker timing NAME` | `preflight` | cwd checks, broker `version` and `build` (a rebuild lands here) |
| `agent-pm-broker: start` | `preflight` | manifest, cwd and `claude_dir` rules, the Claude account's setup-token or OpenCode providers' keys present |
| | `image`, `checks` | the image check; the container-name check and workspace volume (created for `agent` on first use) |
| | `create`, `bootstrap`, `clients` | `docker run`; the client's bootstrap state (none for OpenCode) and the hook stub; every client's `--version` |
| | `client_update` | Codex only: [version preparation](docker-codex.md#container-versions) |
| | `credentials`, `total` | the token injection (OpenCode: its providers' keys); the whole `start` |
| `docker timing NAME GENERATION` | `checks`, `image` | cwd, sweep, the container check; `version` and `build` again (cached), a new start's workspace reset and client home, manifest and plan |
| | `start` | the broker's `start` |
| | `relay`, `tmux`, `client` | supervisor lock and hook relay; the supervisor and the container's tmux session; until the client's pane runs |

`<generation>.result.json` keeps the last block as `phases` and the moment the pane ran as `launched_at` (ms). The
client is ready at its first hook: the first `~/.agent-pm/tui/NAME/hooks.jsonl` record with `ts` after `launched_at`.

### Recovery

Hook events go from the container's hook stub to a relay daemon queueing them in the container, then over
`docker exec` to the host run supervisor, which runs `agent-pm-session hook` for each
([relay protocol](docker-relay-protocol.md)).

```mermaid
flowchart LR
    S[hook stub] --> D[relay serve: queue]
    D --> C[relay connect]
    C --> P[run supervisor] --> H[agent-pm-session hook]
```

- **Supervisor or daemon gone** (killed, not a host reboot): the worker's next `attach`, `read`, `send`, `status`,
  `restart` or `close` starts both and replays the queue; an event arrives once, or twice if its hook ran just before
  the kill, never lost. `next-event` restarts nothing: it reads only the events file. Until the daemon is restarted,
  the hook stub gives up on an event after 10 s and it is lost; `status` still shows the worker running, replies still come from
  the transcript.
- **Refused events**: one for another generation or session dir, a malformed one, or one whose hook exits non-zero
  adds a `rejected.jsonl` line and is not retried.
- **Older image** (by digest, after a plugin upgrade or rebuild): no supervisor restart; the command warns `restart it
  to upgrade`, and `close` does not wait for its supervisor. The first restart attempt after a plugin upgrade runs
  `build` to compare digests, once per generation: `<generation>.stale` records an older image; that first command
  waits for the rebuild.
- **Container gone** (Docker Desktop restart, `docker rm`): the worker shows `dead`; `agent-pm-session resume NAME`
  continues the conversation in a new container. Events queued in the old one are lost.
- **Leftover containers**: before every start and close, a container labelled for a session goes when the session has
  no `~/.agent-pm/tui/NAME/session.json`, it names another generation, or its tmux says the session is gone. A session
  starting or closing elsewhere is skipped. Each running worker of another name costs one `docker exec` (at most 10 s).

## Workspace volumes

A worker's workspace is a Docker volume, `agent-pm-ws-<name>`, never a host directory, where it could plant files
host tools run ([Review before pulling](#review-before-pulling)). It is mounted at the session's cwd; the host path
stays untouched. A worker starts empty, clones with the App's tokens, and its work reaches the host only through
GitHub. npm, uv and pip caches go to `.agent-pm-cache` in it.

The first start creates it, labelled `agent-pm.kind` (`ws`) and `agent-pm.workspace` (the cwd), root owned by `agent`;
every mount sets `volume-nocopy`, so that ownership stays. A volume of that name with other labels, or mounted by
another container, fails the start with `mount_rejected`; clear a foreign one with `docker volume rm`.

```sh
"$B" workspace-rm --volume agent-pm-ws-worker_1           # {"volume":"…","removed":true}
"$B" workspace-rm --volume agent-pm-ws-worker_1 --force   # skips the check
```

Without `--force` a check runs first in the newest worker image, offline, the volume read-only: a repo up to three
levels down with uncommitted, stashed or unpushed work, or a directory it cannot list, keeps the volume: exit 12
`workspace_dirty`, naming each repo and the `--force` command. The check sees only git: ignored files (autopilot plan
docs, progress logs) go with the volume. An absent volume is `removed:false`.

- **New start**: keeps the volume, so a session of the same name finds its checkouts; only a volume mounted at another
  cwd is deleted first, and unsaved work there fails the start, before its config dir is cleared, with that message
  and `or start it in <old cwd> again`.
- **Reclaim**: each `close` (never a start) runs `workspace-rm` on one volume with an `agent-pm.kind` label (protocol
  4's `agent-pm-crew-*` and `agent-pm-run-*` included) whose `agent-pm.workspace` path is gone from the host. A failed
  check keeps it, warns once `docker workspace <volume> kept: …` and leaves `~/.agent-pm/docker-state/.kept/<volume>`:
  unsaved work is never checked again (delete it with `--force` once saved); another failure, not for 24 hours.
- So a crew worker whose cwd stays keeps its volume for good (`workspace-rm` it), and a board issue that switches
  client gets a new name and empty volume; the old one goes when the issue is pruned, so fetch its unpushed work by
  hand.

Checks: [Docker checks › Workspace volumes](docker-checks.md#workspace-volumes).

## Review before pulling

A worker's branch can carry files your tools run. Review a bot PR on GitHub first; only then `gh pr checkout <number>`
in your own clone, and before running anything there read every change to:

- `Makefile` and `package.json` scripts;
- `.claude/` (settings hooks, commands, `CLAUDE.md`) and `.vscode/` (tasks, settings);
- `.envrc`;
- hooks config: `.githooks/`, `.husky/`, `.pre-commit-config.yaml`, `core.hooksPath`, `core.fsmonitor`.

Likewise for an output repo pm and researcher runs push straight to `main` (such as `private_docs`): `git fetch`, then
`git diff --stat HEAD @{upstream}`, before `git pull`. The target repo's `push` and `pull_request` workflows run a
worker's changed scripts with the repo's secrets before you review: require approval for workflow runs, or keep
secrets in environments that need it.

## Live acceptance

`session/tests/live_docker.py` checks the critical path on Docker Desktop, run by hand from
the repo root with `PYTHONPATH` as in [Tests](tests.md). It needs the broker installed and
logged in ([Install](#install), [Log in](#log-in)), for Codex also `install-codex` and
`login codex` ([Codex operations](docker-codex.md#commands)), for OpenCode the host's Ollama on `127.0.0.1` with the
tier model pulled and `OLLAMA_CONTEXT_LENGTH` ≥ 65536, `[session.hosts] default =
"docker"` and `[delegate.delegates] harness_host = "tmux"` ([Run workers](#run-workers),
[Role runs](#role-runs)), and an authorized test repo with `agent-pm-bot` installed.

| Command | Checks |
|---|---|
| `python3 session/tests/live_docker.py worker --client claude\|codex\|opencode CWD [NAME]`, from a manager session | a second build reuses the image; with no `--host`, the worker runs in a container; its security flags (`--cap-drop ALL`, network `bridge`, no extra hosts) and mounts match the plan, and the [unreachable](credentials.md#what-a-container-cannot-reach) resources stay so; a supervisor killed mid-turn delivers its event once; restart, close then resume, and `docker rm -f` then resume each answer a prompt; every close revokes the generation's GitHub tokens and withdraws its client credential; no revocation stays pending but the removed container's, whose tokens it reports; for OpenCode also: the first turn leaves `~/.agent-pm/docker-state/<NAME>/opencode/opencode/opencode.db`, Ollama listens on `127.0.0.1` only (`lsof`), the container holds only GitHub secrets and each configured provider's key (`find /run/agent-pm/secrets`), and each resume continues the same OpenCode session |
| `python3 session/tests/live_docker.py role --repo OWNER/NAME [--timeout S] [NAME]` | a headless engineer run on the test repo gets one forced token refresh, opens a draft PR as `agent-pm-bot[bot]` after it, and its close reclaims its credentials |
| `python3 session/tests/live_docker.py pr URL` | the PR, such as an unattended board run's, was opened as a draft by `agent-pm-bot[bot]` |

Each prints a `PASS` line per check and, per generation, a timing line: [start timing](#start-timing)
phases, client ready, preflight and, for Codex, the `client_update` result. It stops with exit 1 at
the first failed check. The keychain, Linear preset and board-run checks stay manual
([Live checks](credentials.md#live-checks)).

## Risks

- Container credentials are readable by every process in the container and by any process running as you
  ([Credentials](credentials.md)); each restart and resume writes them into a new container.
- A broker reinstall needs all three logins, each `login claude <name>` and each `login opencode <name>` again
  ([Log in](#log-in)); until then starts fail (`login_required`, or `keychain_failed` for the GitHub items).
- An OpenCode provider at an `http://` `base_url` gets its API key and every prompt in plaintext: anyone on the LAN
  path can capture them. Prefer `https://`, or a key that only that server accepts.
- A GitHub token the broker could not revoke stays valid until it expires (an hour); `"$B" status` lists it.
- Every Docker session can push to any unprotected branch of every repo the App is installed on, `private_docs`'s
  `main` included.
- The [audit log](#audit-log) is yours to change: it records what the broker did, not that nothing else happened.
- Git in a container runs each checkout's own `.githooks/` (`core.hooksPath`), as `agent`, on its branch's
  version: a target repo's hooks run with the container's GitHub tokens in reach.
- Image builds trust the client vendors' npm packages ([Codex's policy](docker-codex.md#container-versions)).
- A resume after an upgrade runs a newer Claude than the one that wrote the conversation; if it cannot read it, the
  resume fails rather than starting anew. Claude may show an onboarding prompt after a rebuild; the
  [Claude check](docker-checks.md#start) catches it.
- Docker Desktop file sharing must include `~/.agent-pm/docker-state` (Settings > Resources > File sharing), or start
  fails with `start_failed`.
- A kept dirty volume takes disk until you act. Volumes are plain files in the Docker Desktop VM: anyone with Docker
  access reads them.
- `claude_dir` is a read-write bind: a worker can leave settings, hooks, `CLAUDE.md`, symlinks or large files there for
  its later generations; host readers must not follow symlinks.
- A headless run's output comes from its container, where client and tools share uid `agent`: a forged rate-limit line
  pauses the board for that client until its reset time, as a forged transcript record can for a TUI run. The harness
  holds up to 66 lines of up to 8 Mi characters each, both streams together (about 2 GiB at worst as Python strings),
  before reading them.
- A harness killed with `kill -9` leaves its headless client running, with its token, until the next Docker start or
  close.
- Every container, Claude, Codex and OpenCode workers included, reaches through `host.docker.internal` the Mac's services that
  listen only on `127.0.0.1`: on 2026-10-18 a worker-like container (bridge, `--cap-drop ALL`, no mounts or
  credentials) got Ollama's `/api/version` while Ollama listened only on `127.0.0.1:11434`. The host side of such a
  connection belongs to `com.docker.backend`, running as you, so pf cannot tell container traffic apart
  ([Docker networking](https://docs.docker.com/desktop/features/networking/)); the Mac has no Docker gateway address
  to listen on instead: docker0 lives inside the VM
  ([Docker networking how-tos](https://docs.docker.com/desktop/features/networking/networking-how-tos/)). The user
  accepted this risk on 2026-10-18 (TASK-695 FR-24): no forwarder, firewall or Docker Business.
