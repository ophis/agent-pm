# Credentials

Every credential agent-pm uses: who logs in, what each is for, where it is stored, who can read it, how long it lives
and how to revoke it. Local Hosts (`tmux`, `pty`) and the Docker Host (`[session.hosts] default = "docker"`,
[Docker host](docker-host.md)) differ for the model clients; their columns are separate. Sources are `path:line` in
this repo or the official docs linked at the end. Requirement IDs (`TASK-549 FR-7`) are the Docker Host PRDs' of
TASK-545, TASK-548, TASK-549 and TASK-550. `<B>` is `/usr/local/libexec/agent-pm/agent-pm-broker`. This page holds no
token, key or private-key value; `tests/guard_checks/credentials.py` checks it names every credential, label and
state config key and every broker keychain service.

**Three-way readers**: a credential the broker hands to a container (the Claude setup-token copy, GitHub installation
tokens, the Codex access token) can be read by the broker, by every process in that container, and by any process
running as the user: `<B> start --manifest <file>` does not check its caller (`broker/main.go:118`), and the Docker
socket is the user's (`broker/internal/docker/docker.go:102`), so such a process can start a container or
`docker exec` into one and read `/run/agent-pm/secrets/`. Only the App private key and the Codex refresh token stay
with the broker. The user accepted this on 2026-10-18; the broker is unchanged ([Risks](#risks)).

## Who logs in

| Host | Client | Who logs in, how | Login state | Container gets | The copy goes |
|---|---|---|---|---|---|
| `tmux`, `pty` | Claude Code | the user, beforehand: `claude`, then `/login` ([Claude auth]) | macOS Keychain; `~/.claude/.credentials.json` (0600) if the Keychain refuses the write ([Claude auth]) | no container | — |
| `tmux`, `pty` | Codex | the user, beforehand: `codex login` ([Codex auth]) | `~/.codex/auth.json`, or the OS credential store per `cli_auth_credentials_store` ([Codex auth]) | no container | — |
| Docker | Claude Code | the user, once per broker install: `claude setup-token`, pasted into `<B> login claude` ([Log in](docker-host.md#log-in)) | `agent-pm-claude` / `setup-token` | `/run/agent-pm/secrets/claude`, 0400 on tmpfs, given to the client only as `CLAUDE_CODE_OAUTH_TOKEN` (`broker/internal/broker/keychain.go:423`, `broker/internal/launcher/assets/with-secrets:21`) | deleted with the container (close, restart, resume start a new one); never revoked |
| Docker | Codex | an admin installs the pinned Codex (`sudo <B> install-codex <version>`); the user, once per broker install: `<B> login codex` in a browser ([Codex operations](docker-codex.md#commands)) | `agent-pm-codex` / `auth` | `/run/agent-pm/secrets/codex/auth.json`, 0400 on tmpfs, with no refresh token (`broker/internal/broker/codex.go:397`); the broker publishes each renewal | deleted with the container; never revoked |

GitHub on the Docker Host needs no per-run login: `<B> login github agent-pm-bot` once per broker install
([Log in](docker-host.md#log-in)). Local Hosts use the host `gh` login below.

## Credentials

### Host gh login

| Field | Value | Source |
|---|---|---|
| Purpose | the orchestrator's target-repo check, docs fetch and duplicate search; the Delegate's ready gate (`gh pr ready`, `--undo`); `repo.py` clone, PR and API calls; the manager's PR reads | `linear-orchestrator/src/agent_pm/linear_orchestrator/engine/target.py:79`, `…/engine/related.py:177`, `delegate/src/agent_pm/delegate/engine/ready_gate.py:86`, `delegate/src/agent_pm/delegate/team/tools/repo.py:146`, `manager/src/agent_pm/manager/engine/crew.py:541` |
| Minimum permission | an account that can push to and open pull requests on every target repo; `gh auth login`'s scopes (a classic token needs at least `repo`, `read:org`, `gist`) | [gh auth login] |
| Stored at | the system credential store, else a plain-text file; `gh auth status` says which | [gh auth login] |
| Readers | any process running as the user (`gh auth token`); never mounted into a container | [gh auth login]; [What a container cannot reach](#what-a-container-cannot-reach) |
| Lifetime | until revoked | [gh auth logout] |
| Revoke | `gh auth logout` removes it locally only; revoke at github.com › Settings › Applications › GitHub CLI › Revoke access (every token gh ever made) | [gh auth logout] |

### agent-pm-bot App private key

| Field | Value | Source |
|---|---|---|
| Purpose | signs the 9-minute JWT that issues installation tokens; never leaves the broker | `broker/internal/github/github.go:96`, `broker/internal/broker/github.go:319` |
| Minimum permission | one private App, installed only on repos agents may push to; the broker requests contents write, pull requests write, checks read, actions read | `broker/internal/github/github.go:37`; [Log in](docker-host.md#log-in) |
| Stored at | login keychain, service `agent-pm-bot` (`[session.hosts.docker.github] app`), account the App ID, created by the user; delete the downloaded `.pem` once the item holds it | `broker/internal/broker/keychain.go:57`, `session/src/agent_pm/session/config.toml:127` |
| Readers | the broker only, once `<B> login github agent-pm-bot` has made the item trust only it; before that the user-created item trusts `/usr/bin/security`, so any process running as the user; and so does a `.pem` left on disk | `broker/internal/broker/keychain.go:316`; [Log in](docker-host.md#log-in) |
| Lifetime | until the key is deleted in the App's settings | [GitHub private keys] |
| Revoke | generate a new key and delete the old one in the App's settings; replace the item's content; rerun `<B> login github agent-pm-bot` | [GitHub private keys] |

### GitHub installation tokens

| Field | Value | Source |
|---|---|---|
| Purpose | git (credential helper) and `gh` (wrapper setting `GH_TOKEN` for its own process) inside a Docker container | `broker/internal/launcher/assets/gh:76`; [What a container gets](docker-host.md#what-a-container-gets) |
| Minimum permission | one token per installation, covering every repo of it, with the four permissions above | `broker/internal/github/github.go:178`, `broker/internal/broker/github.go:379` |
| Stored at | broker items `agent-pm-tokens` (per container) and `agent-pm-revocations` (backlog); the container's `/run/agent-pm/secrets/github/<owner>` (0400, tmpfs) | `broker/internal/broker/github.go:25` |
| Readers | **three-way readers** | intro |
| Lifetime | 1 hour; each session renews them 10 minutes before the first expires | [GitHub installation tokens]; `session/src/agent_pm/session/hosts/docker/supervisor.py:35` |
| Revoke | `close` revokes them (`DELETE /installation/token`) before removing the container; a failed revoke is retried at the next start, close or status until it expires; `<B> status` lists pending ones | `broker/internal/github/github.go:191`, `broker/internal/broker/github.go:469`, `broker/internal/broker/github.go:491`; [GitHub revoke] |

### Linear harness key

| Field | Value | Source |
|---|---|---|
| Purpose | the harness account's access for router (claims, bounces), promote (creates the next role's issue, relates it, moves to Done), prune (archives) and run comments | `linear-orchestrator/src/agent_pm/linear_orchestrator/engine/router.py:1186`, `…/engine/promote.py:366`, `…/engine/prune.py:467`, `…/engine/writeback.py:451` |
| Minimum permission | a personal API key of the harness account limited to the team, with Read, Write, Create issues and Create comments (from the mutations it sends: `issueUpdate`, `issueCreate`, `issueRelationCreate`, `issueArchive`, `commentCreate`, `commentUpdate`); not live-tested | `…/engine/promote.py:90`, `…/engine/prune.py:76`, `…/engine/ops.py:20`, `…/engine/run_comments.py:108`; [Linear API keys] |
| Stored at | a login keychain item whose service is `harness_key` | `linear-orchestrator/src/agent_pm/linear_orchestrator/config.toml:17` |
| Readers | any process running as the user: read through `/usr/bin/security`, which an item made by `security add-generic-password` without `-T` trusts | `linear-orchestrator/src/agent_pm/linear_orchestrator/engine/client.py:14` |
| Lifetime | no expiry documented | [Linear API keys] |
| Revoke | delete the key in Linear › Settings › Account › Security & Access, then `security delete-generic-password -s <service>` | [Linear API keys] |

### Linear role keys

One per role: `roles.researcher.key` for `roles.researcher.account`, `roles.pm.key` for `roles.pm.account`,
`roles.engineer.key` for `roles.engineer.account`.

| Field | Value | Source |
|---|---|---|
| Purpose | write-back as the role: comments, state moves, subscribing `human_members`, attachments, retitles; the router checks the item exists before a claim | `…/engine/writeback.py:101`, `…/engine/ops.py:27`, `…/engine/ops.py:30`, `…/engine/router.py:606` |
| Minimum permission | a personal API key of the role account limited to the team, with Read, Write and Create comments (from the mutations above); not live-tested | `…/engine/writeback.py:76`, `…/engine/writeback.py:78`; [Linear API keys] |
| Stored at | a login keychain item whose service is `roles.<role>.key`; the router records the key's service in each run's start row | `…/config.toml:83`, `…/engine/router.py:676` |
| Readers | as the harness key | `linear-orchestrator/src/agent_pm/linear_orchestrator/engine/client.py:14` |
| Lifetime | as the harness key | [Linear API keys] |
| Revoke | as the harness key | [Linear API keys] |

### Claude Code login

| Field | Local (`tmux`, `pty`) | Docker | Source |
|---|---|---|---|
| Purpose | the user's own Claude Code | worker and role-run Claude Code in a container | [Who logs in](#who-logs-in) |
| Minimum permission | the user's subscription login | a setup-token: subscription, model requests only | [Claude auth] |
| Stored at | macOS Keychain (fallback `~/.claude/.credentials.json`) | `agent-pm-claude` / `setup-token`; one copy per running container | `broker/internal/broker/keychain.go:28`, `broker/internal/broker/keychain.go:423` |
| Readers | Claude Code and any process running as the user that the Keychain lets read it | the item: the broker; the copies: **three-way readers** | [Claude auth]; intro |
| Lifetime | refreshed by Claude Code; `/login` renews | one year | [Claude auth] |
| Revoke | `/logout` | Unknown: no official route. `claude.ai/settings/claude-code`, cited by the community, is unverified. On a leak: run `claude setup-token` again and rerun `<B> login claude` | [Claude auth] |

### Codex login

Docker uses the decided path A, centralized refresh ([Plan A validation report](docker-codex.md#plan-a-validation-report)).

| Field | Local (`tmux`, `pty`) | Docker | Source |
|---|---|---|---|
| Purpose | the user's own Codex | worker and role-run Codex in a container | [Codex operations](docker-codex.md) |
| Minimum permission | the user's ChatGPT login | the broker's own ChatGPT login; the container gets an access token without a refresh token | `broker/internal/broker/codex.go:397` |
| Stored at | `~/.codex/auth.json` or the OS store | the full login, refresh token included: `agent-pm-codex` / `auth`; the access token: `/run/agent-pm/secrets/codex/auth.json` per container | `broker/internal/broker/codex.go:24`; [Docker sessions](docker-codex.md#docker-sessions) |
| Readers | Codex and any process running as the user | refresh token: the broker only, except that during `login codex` and each renewal the full login also sits in a 0700 temp dir under `/private/tmp` that any process running as the user can read; access token: **three-way readers** | `broker/internal/broker/codex.go:214`, `broker/internal/broker/codex_refresh.go:19`; [Login trust](docker-codex.md#login-trust) |
| Lifetime | refreshed by Codex during use | access token renewed by the broker 10 minutes before it expires; refresh token: not documented | `broker/internal/broker/codex_refresh.go:16`; [Codex auth] |
| Revoke | `codex logout` clears it locally | Unknown upstream: no documented route. Rerun `<B> login codex` to replace the login, or delete the `agent-pm-codex` item | [Codex auth] |

## Keychain items

All in the login keychain. "Readers" is by design; [Measured readers](#measured-readers) records what a live read
shows.

| Service | Account | Config key | Holds | Written by | Readers |
|---|---|---|---|---|---|
| `agent-pm-claude` | `setup-token` | — | Claude setup-token | `<B> login claude` | the broker |
| `agent-pm-bot` | App ID | `[session.hosts.docker.github] app` | App private key | the user; access set by `<B> login github agent-pm-bot` | the broker, once logged in |
| `agent-pm-github-users` | `agent-pm-bot` | — | the bot user's ID, for git identity (not a secret) | `<B> login github agent-pm-bot` | the broker |
| `agent-pm-tokens` | container name | — | that container's installation tokens and expiries | `<B> start`, `refresh`; removed by `close` | the broker |
| `agent-pm-revocations` | `pending` | — | tokens awaiting revocation | `<B> close`, `refresh` failures | the broker |
| `agent-pm-codex` | `auth` | — | the full Codex login | `<B> login codex`; renewals at `start`, `refresh` | the broker |
| the `harness_key` value | any | `harness_key` | harness Linear API key | the user ([Linear presets](#linear-presets)) | any process running as the user |
| each `roles.<role>.key` value | the role's account | `roles.<role>.key` | role Linear API key | the user | any process running as the user |
| Claude Code's own | — | — | local Claude login | Claude Code `/login` | Claude Code; service name not documented |
| `gh`'s own | — | — | host `gh` token | `gh auth login` | `gh`; see `gh auth status` |
| Codex's own, keyring mode only | — | — | local Codex login | `codex login` | Codex; service name not documented |

Claude setup-token, container copy: `/run/agent-pm/secrets/claude` in every running Claude container; readable by
every process in the container and every process that can `docker exec` (**three-way readers**).

The broker's services are `KeychainServices` (`broker/internal/broker/keychain.go:61`).

### Measured readers

Each broker item is read twice: from a `/agent-pm:manage` session's shell tool (manager) and from Terminal (shell);
both read through `security`, which is how a process reads a generic password, so they cover the manager, a shell and
`security`. First check the item exists (attributes only, no value), then read it; neither prints the value, and
`perl … alarm 10` stands for `timeout 10`, which macOS lacks:

```sh
security find-generic-password -s <service> >/dev/null 2>&1; echo "exists=$?"
bash -c 'perl -e "alarm 10; exec @ARGV" security find-generic-password -s <service> -w 2>/dev/null | wc -c; echo "exit=${PIPESTATUS[0]}"'
```

An item must exist to be measured (`exists=0`); `exists=44` means absent: record `absent, not measured` and make it
first. `agent-pm-claude`, `agent-pm-github-users` and `agent-pm-codex` exist after their `login`; `agent-pm-bot` once
the user stores the key; `agent-pm-tokens` while a Docker session runs; `agent-pm-revocations` after a `close` with the
network off, until the next start, close or status online.

Pass: `exists=0`, `exit=` non-zero (142 when the alarm stops it) and a byte count of `0`. Click Deny on any dialog, never Always
Allow; if Always Allow was clicked, `/usr/bin/security` now reads the item: rerun the `login` that writes it
(`login github agent-pm-bot` for `agent-pm-bot` and `agent-pm-github-users`), or for `agent-pm-tokens` and
`agent-pm-revocations` delete the item as [Upgrading](upgrading.md) says, and note it here. Record only
`exists=<n>, exit=<n>, bytes=<n>`.
A read that returns bytes fails this check unless the user accepts it: mark the row `silently readable, accepted <date>`.

| Service | Manager | Shell |
|---|---|---|
| `agent-pm-claude` | pending | pending |
| `agent-pm-bot` | pending | pending |
| `agent-pm-github-users` | pending | pending |
| `agent-pm-tokens` | pending | pending |
| `agent-pm-revocations` | pending | pending |
| `agent-pm-codex` | pending | pending |

## Broker issues

| Issued | To | Lifetime | Recalled | Requirements |
|---|---|---|---|---|
| Claude setup-token copy | `/run/agent-pm/secrets/claude`, through `docker exec` stdin; the client's environment only | until its container goes; the token itself a year | deleted with the container; not revoked | TASK-545 FR-8, FR-13; TASK-548 FR-12 |
| GitHub installation tokens | `/run/agent-pm/secrets/github/<owner>`, one per installation | 1 hour, renewed 10 minutes ahead | revoked at `close`; backlog retried | TASK-549 FR-7 to FR-12 |
| Codex access token, no refresh token | `/run/agent-pm/secrets/codex/auth.json`; `CODEX_HOME/auth.json` links to it | renewed 10 minutes ahead | deleted with the container; not revoked | TASK-550 FR-3 to FR-7, FR-12 |
| Bot user ID (not a secret) | the session's git author and committer identity | — | — | TASK-549 FR-14, FR-26 |

## What a container reaches

| Resource | Requirements |
|---|---|
| its workspace volume `agent-pm-ws-<name>` at its cwd (`broker/internal/launcher/start.go:166`) | TASK-549 FR-4, FR-17 |
| Claude: its `claude_dir` bind; Codex: `CODEX_HOME` on tmpfs and its `sessions/` bind (`broker/internal/launcher/start.go:169`) | TASK-549 FR-4; TASK-550 NFR-4 |
| `/run/agent-pm/secrets/` (tmpfs, 0700, `agent`) and what [Broker issues](#broker-issues) puts there (`broker/internal/launcher/start.go:177`) | TASK-545 FR-13; TASK-549 FR-8 |
| the network, the host's `host.docker.internal` included | TASK-545 FR-10 |
| push to every repo of every App installation | TASK-549 FR-7 |

Every container runs `--cap-drop ALL`, `--security-opt no-new-privileges`, `--user agent`
(`broker/internal/launcher/start.go:146`): TASK-545 FR-10.

## What a container cannot reach

Run each with `C` a running worker container (`docker ps --filter label=agent-pm.session --format '{{.Names}}'`),
once for a Claude and once for a Codex worker. Each prints counts only.

| Resource | Command | Pass | Requirements | Claude | Codex |
|---|---|---|---|---|---|
| host keychain | `docker exec "$C" sh -c 'find / \( -path /proc -o -path /sys \) -prune -o -path "*/Library/Keychains*" -print 2>/dev/null \| wc -l; command -v security \| wc -c'` | `0`, `0` | TASK-549 NFR-2 | pending | pending |
| Linear keys | `docker exec "$C" sh -c 'env \| grep -ci linear; find / \( -path /proc -o -path /sys \) -prune -o -name config.local.toml -print 2>/dev/null \| wc -l'` | `0`, `0` | TASK-549 NFR-2 | pending | pending |
| host `gh` login | `docker exec "$C" sh -c 'find / \( -path /proc -o -path /sys \) -prune -o -path "*/gh/hosts.yml" -print 2>/dev/null \| wc -l; env \| grep -c "^GH_TOKEN="'` | `0`, `0` | TASK-549 NFR-2 | pending | pending |
| Docker socket | `docker exec "$C" sh -c 'find / \( -path /proc -o -path /sys \) -prune -o -type s -name "*docker*" -print 2>/dev/null \| wc -l; command -v docker \| wc -c; env \| grep -c "^DOCKER_HOST="'` | `0`, `0`, `0` | TASK-545 FR-10; TASK-549 NFR-2; TASK-550 NFR-4 | pending | pending |

Each search covers every mount (the workspace volume, the binds, the tmpfs), skipping only `/proc` and `/sys`. The
container's own tmux socket is not Docker's, so the socket search names `docker`.

## Linear presets

Create these in the Linear team, then put each id in `~/.agent-pm/config.local.toml` under `[linear_orchestrator]`
([Setup](board.md#setup)).

| Create in Linear | Config key |
|---|---|
| the team | `team` |
| workflow states Todo, In Progress, In Review, Handoff, Done, Canceled | `states.todo`, `states.in_progress`, `states.in_review`, `states.handoff`, `states.done`, `states.canceled` |
| a `Tasks` label group, its id from `issueLabels { nodes { id name isGroup } }` | `task_label_group` |
| one label per task under `Tasks` (`build`, `light-build`, …), optional | `task_labels` |
| an `Agent` label group and one label per client, optional; set both or neither | `agent_label_group`, `agent_labels` |
| one member account per role, a plus-alias such as `you+pm@example.com` | `roles.researcher.account`, `roles.pm.account`, `roles.engineer.account` |
| the humans subscribed on In Review | `human_members` |
| the Linear project of each product repo, optional | `project_repos` |
| — (local clones of target repos, optional) | `local_clones` |
| the harness account's API key, a Keychain item (below) | `harness_key` |
| each role account's API key, a Keychain item (below) | `roles.researcher.key`, `roles.pm.key`, `roles.engineer.key` |

Keys, one per account:

1. Linear › Settings › Members: invite each role account; open the invite in a private window and choose
   **Continue with email**.
2. As each account, the harness account included, create a personal API key in Settings › Account › Security & Access,
   limited to the team ([Linear API keys]).
3. Store each in the login keychain, then paste the key (nothing echoes):

   ```sh
   security add-generic-password -s <roles.<role>.key> -a <roles.<role>.account> -w   # each role
   security add-generic-password -s <harness_key> -a <harness account> -w
   ```

4. `bin/agent-pm-linear router --dry-run` and `bin/agent-pm-linear promote --dry-run` report no config error.

## Live checks

For the manager and the user, on macOS with Docker Desktop; write each result in this page.

- [ ] [Measured readers](#measured-readers): six services, manager and shell.
- [ ] [What a container cannot reach](#what-a-container-cannot-reach): four commands, a Claude and a Codex worker.
- [ ] [Linear presets](#linear-presets): in a new Linear team, only this section and the config; both dry-runs report
  no config error.
- [ ] [Live acceptance](docker-host.md#live-acceptance): `worker` for Claude and Codex, `role`, and `pr` on an unattended
  board run's PR, each exits 0.

## Risks

- **Three-way readers** of handed-out credentials: accepted by the user on 2026-10-18; the broker is unchanged.
- The Linear key items trust `/usr/bin/security`: any process running as the user reads them without a dialog.
- Each broker release changes its ad-hoc code identity: rerun `login claude`, `login github agent-pm-bot` and
  `login codex` ([Log in](docker-host.md#log-in)).
- Neither the setup-token nor the Codex login has a documented upstream revocation.

[Claude auth]: https://code.claude.com/docs/en/authentication#credential-management
[Codex auth]: https://learn.chatgpt.com/docs/auth#login-caching
[gh auth login]: https://cli.github.com/manual/gh_auth_login
[gh auth logout]: https://cli.github.com/manual/gh_auth_logout
[GitHub private keys]: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps
[GitHub installation tokens]: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app
[GitHub revoke]: https://docs.github.com/en/rest/apps/installations#revoke-an-installation-access-token
[Linear API keys]: https://linear.app/docs/api-and-webhooks#api-keys
