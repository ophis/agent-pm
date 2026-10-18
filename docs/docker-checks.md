# Docker checks

Hand checks for the [Docker host](docker-host.md) and [Codex operations](docker-codex.md), run on macOS (Apple
silicon) with Docker Desktop, each after the step its section names. `B` is
`/usr/local/libexec/agent-pm/agent-pm-broker`, `PKG` the installed agent-pm plugin directory, `I` the image
[`build`](docker-host.md#build) prints and `C` the container [`start`](docker-host.md#start) prints.

## Install

After [Install](docker-host.md#install).

| Check | Expect |
|---|---|
| `codesign -dv --verbose=4 "$B" 2>&1 \| grep -E 'Identifier\|flags'` | `Identifier=com.github.ophis.agent-pm-broker`, `flags=0x10002(adhoc,runtime)` |
| `ls -ld /usr/local/libexec/agent-pm "$B"` | both `root wheel`, `drwxr-xr-x` / `-rwxr-xr-x` |
| `"$B" version` | `{"protocol_version":"14"}` |
| edit `~/.agent-pm/docker/worker/Dockerfile`, rerun `install.sh` | your edit stays |

## Claude login

After `login claude` ([Log in](docker-host.md#log-in)).

| Check | Expect |
|---|---|
| output | `Claude setup-token saved; keychain trusts only the broker`, no token |
| `security dump-keychain -a ~/Library/Keychains/login.keychain-db \| grep -A20 agent-pm-claude` | service `agent-pm-claude`, account `setup-token`; the only trusted application is `$B` |
| shell history | no token |
| first Claude [start](docker-host.md#start) after it | no keychain dialog |

## GitHub login

After `login github agent-pm-bot` ([Log in](docker-host.md#log-in)).

| Check | Expect |
|---|---|
| first run after an install | one hidden `Login keychain password` question; no macOS dialog, or one only after the password |
| output | `GitHub App agent-pm-bot private key (App ID <App ID>) now trusts only the broker`, then `GitHub App agent-pm-bot bot user agent-pm-bot[bot] has ID <ID>` (`gh api 'users/agent-pm-bot[bot]' --jq .id`), no key |
| `security dump-keychain -a ~/Library/Keychains/login.keychain-db \| grep -A40 '"agent-pm-bot"'` | every entry with `decrypt` or `export` lists only `$B`, none `/usr/bin/security`; `partition_id` lists only the `cdhash:` from `codesign -dv --verbose=4 "$B"` (its `CDHash=`), no `apple-tool:` |
| `security find-generic-password -s agent-pm-bot -a <App ID> -w` | asks for the keychain password; Cancel: non-zero exit, empty stdout |
| rerun the login | same output, no password question |

## Build

After [Build](docker-host.md#build).

| Check (`I` = the printed image) | Expect |
|---|---|
| `docker image inspect "$I" --format '{{json .Config.Labels}}'` | `agent-pm.version` equals the plugin's version; `agent-pm.key`, `agent-pm.agent-uid`, `agent-pm.agent-gid` |
| `docker history --no-trunc "$I" \| grep -c sk-ant` | `0` |
| `docker run --rm "$I" sh -c 'command -v python3; python3 -P /opt/agent-pm/team/tools/repo.py --help'` | `/opt/agent-pm/bin/python3` (it imports the Delegate), then `repo.py`'s usage: role runs use the plugin's own team at `/opt/agent-pm/team` |
| delete a line (e.g. `WORKDIR`) from your Dockerfile, rebuild | still the labels and `/opt/agent-pm/bin/with-secrets`; restore the line |
| `docker image ls --filter reference='agent-pm-*'` | the new pair and the older one, leased within the hour |
| `rm ~/.agent-pm/docker/images/<older key>`, rebuild with `--max-age 0`, list again | only the new pair, plus an older worker image a container still uses |
| `"$B" build --agent-pm "$PKG" --max-age 0 & "$B" build --agent-pm "$PKG2" --max-age 0; wait` (`PKG2` another worktree's, other content) | both `"rebuilt":true`; both pairs listed |
| `"$B" build --agent-pm "$PKG" --max-age 0` | `"rebuilt":true`, same `image`, new `digest`; `npm install` runs, not `CACHED` |

## Plugins

After a `--plugin` build ([Plugins](docker-host.md#plugins)).

| Check | Expect |
|---|---|
| `"$B" build --agent-pm "$PKG" --plugin autopilot`, then `docker run --rm "$I" sh -c 'echo "$CLAUDE_CODE_PLUGIN_DIRS"; stat -c %U /opt/agent-pm/plugins/autopilot; touch /opt/agent-pm/plugins/autopilot/x'` | `/opt/agent-pm/plugins/autopilot`, `root`, `Permission denied` |
| `"$B" build --agent-pm "$PKG" --plugin no-such-plugin` | exit 8, `build_failed`, `Plugin no-such-plugin is not installed: …`; no Docker build |
| `claude plugin update autopilot@<marketplace>`, the same build | `"rebuilt":true`, a new key |
| `/autopilot:build` in a container's Claude | the skill starts |

## Start

After [Start](docker-host.md#start), with the manifest there.

| Check | Expect |
|---|---|
| `docker inspect "$C" --format '{{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.Config.User}} {{.HostConfig.Privileged}} {{.HostConfig.NetworkMode}} {{.HostConfig.PidMode}}'` | `[ALL] [no-new-privileges] agent false bridge ` (empty PidMode) |
| `docker inspect "$C" --format '{{json .Mounts}}'` | the volume `agent-pm-ws-worker_1` at cwd, one bind (claude_dir), no `docker.sock` |
| `docker exec "$C" grep -E "$HOME\|/run/agent-pm" /proc/mounts` | the volume at cwd, claude_dir at its host path, the `/run/agent-pm/secrets` tmpfs; nothing else from the host |
| `docker volume inspect agent-pm-ws-worker_1 --format '{{json .Labels}}'` | `agent-pm.kind` `ws`, `agent-pm.workspace` `$HOME/src/demo` |
| `docker exec "$C" sh -c 'touch x; stat -c %U .; echo $npm_config_cache'`, then `ls ~/src/demo` | `agent`, `$HOME/src/demo/.agent-pm-cache`; the host dir stays empty |
| `docker exec "$C" ps -o comm= -p 1` | `tini` |
| `docker exec "$C" sh -c 'id -u; command -v sudo; tmux -V; git --version; gh --version; python3 --version; uv --version; node --version; claude --version; codex --version'` | uid not 0, no sudo, every version prints |
| `docker exec "$C" ls -l /run/agent-pm/secrets` | one file `claude`, `-r--------`, owned by `agent` |
| `docker inspect "$C" \| grep -c sk-ant`; `docker logs "$C" \| grep -c sk-ant`; `grep -c sk-ant /tmp/worker_1.json`; `docker exec "$C" env \| grep -c sk-ant` | `0` each |
| `cat ~/.agent-pm/docker-state/worker_1/claude/.claude.json` | `hasCompletedOnboarding: true`, `projects["$HOME/src/demo"].hasTrustDialogAccepted: true` |
| `cat ~/.agent-pm/docker-state/worker_1/claude/settings.json` | the fixed values of [Lifecycle](docker-host.md#lifecycle) |
| `lsof -nP -a -iTCP -sTCP:LISTEN -c agent-pm` during a start | no listening TCP socket (Codex preparation and GitHub calls may open outbound HTTPS) |
| with `"github": {"app": "agent-pm-bot"}` in the manifest | start's `github` lists one `{"account", "expires_at"}` per installation, `github_user_id` the ID `login github` saved |
| then `docker exec "$C" ls -l /run/agent-pm/secrets/github` | one file per installation account, `-r--------`, owned by `agent` |
| `docker exec "$C" git ls-remote https://github.com/<account>/<private repo>`; `docker exec "$C" gh api repos/<account>/<private repo> --jq .full_name` | both work with no login |
| `docker exec -w <clone of a private repo> "$C" gh api 'repos/{owner}/{repo}' --jq .full_name` | works with no login (`{owner}` from `origin`) |
| `"$B" refresh --container "$C" --manifest /tmp/worker_1.json` | a new `github` list, each `expires_at` later |
| `"$B" close --container "$C"`, then `"$B" status` | `{"revocations":[]}` |

**Claude**: `docker exec -it "$C" with-secrets --client claude claude` opens with no login, theme or trust prompt and
answers one sentence.

**Hijack**: with `export DOCKER_HOST=tcp://127.0.0.1:9 DOCKER_CONTEXT=bogus`, a `~/.docker/config.json` naming another
context, and an executable `docker` first on `PATH` that runs `touch /tmp/fake-docker-ran`, `"$B" build --agent-pm
"$PKG"` still reports `"rebuilt":false` and `/tmp/fake-docker-ran` does not exist. Restore `~/.docker/config.json`.

## Refusals

Each fails before any container exists (`docker ps -a` unchanged):

| Change to the manifest or host | Exit |
|---|---|
| add a field `"x": 1` | 3 `manifest_rejected` |
| `image` a tag not built by `build` (e.g. `agent-pm-worker:` + 64 `0`) | 6 `image_rejected` |
| `cwd` a missing dir, `/`, `/home`, `/usr/local/x`, `/opt/agent-pm`, `/home/agent` or `$HOME` (it holds `claude_dir`) | 4 `mount_rejected` |
| `claude_dir` any other path | 4 |
| `hook_path` inside `cwd` | 4 |
| `security delete-generic-password -s agent-pm-claude -a setup-token ~/Library/Keychains/login.keychain-db`, then start | 5 `login_required`, naming `agent-pm-broker login claude`; log in again after |
| reinstall the broker (`install.sh --build`), then start a Claude manifest without `login claude` | 5 `login_required`, "trusts another broker build"; no keychain dialog; log in again after |
| `security lock-keychain ~/Library/Keychains/login.keychain-db`, then start a Claude manifest | 5 `login_required`, "unlock the login keychain, or run agent-pm-broker login claude"; no dialog; `security unlock-keychain ~/Library/Keychains/login.keychain-db` after |
| start the same manifest while its container runs | 9 `start_failed`, "close it first"; the running container untouched |
| while `C` runs, the same manifest with a new `generation` | 4 `mount_rejected`: its volume is in use |
| `docker volume create agent-pm-ws-worker_3`, then a `worker_3` manifest | 4 `mount_rejected`: no workspace labels; `docker volume rm agent-pm-ws-worker_3` after |

## Workspace volumes

See [Workspace volumes](docker-host.md#workspace-volumes).

| Check | Expect |
|---|---|
| in `C`, `git clone` a public repo into `src/x` and add a file; close; `"$B" workspace-rm --volume agent-pm-ws-worker_1` | exit 12, naming `src/x`; `docker volume ls` still lists it |
| `docker ps -a --filter label=agent-pm.check` during and after | the check container while it runs, nothing after |

## Codex live checks

For the manager and the user on authorized macOS Apple silicon with Docker Desktop, after release; workers run
none. Use an isolated login, versions meeting the minimum, and record only versions, times, statuses, counts and
redacted errors: never dump auth, keychain, environment or token values, change credential dates or test real
expiry. Missing observations stay incomplete. Final expiry and close retries are covered with fake clocks and
credentials.

Owners: [PR #408](https://github.com/ophis/agent-pm/pull/408), [PR #409](https://github.com/ophis/agent-pm/pull/409)
and [PR #411](https://github.com/ophis/agent-pm/pull/411)'s live checklists:

| Check | Expect |
|---|---|
| Start two Codex crew workers, a terminal role run and an unattended Codex board role run | no login/onboarding prompt; replies reach `crew report` and completion events; no role-specific manifest |
| Hide personal host Codex from `PATH` for these | both modes and container hook trust still start, from image files |
| Inspect mounts/security without dumping env or secrets | one workspace volume; only the client history bind; volatile home/secrets; user `agent`, CapDrop ALL, no-new-privileges; no Docker socket, privileged mode or host namespaces |
| Restart, then close and resume a worker | same native identity and history; no duplicate completed event; missing history fails |
| Finish a headless run, reopen it as a terminal, close, resume headless on the same Host | same native ID, `CODEX_HOME`, history and workspace; replies and outcomes once |
| Restart the host supervisor, then query/send | hook relay resumes, same identity, published deadline kept |
| After native credential replacement and close, check state | host client dir holds only allowed history; no auth/token file or value in host state or workspace; personal login unchanged |
| Start, restart, close/resume with supported pinned/image versions and a newer registry stable | each generation runs registry latest; audit and progress agree; program outside workspace/history |
| Repeat with the registry unavailable, then an unavailable/corrupt supported update | image version restored and recorded; no secret injected before preparation |
| Below-minimum image, then below-minimum pinned host; then equal and higher versions | low versions refused before any secret, with rebuild/upgrade instructions; equal/higher admitted; a failed advisory lookup still starts |
| Close after a successful update, then resume | executable tmpfs gone; next generation selects again; native history kept |
| Use a whitelisted skill in both modes | namespaced skill runs from the image, its scripts/references resolve; empty whitelist loads none |

Owner: [TASK-650](https://linear.app/ophis-workgroup/issue/TASK-650), concurrent refresh (none recorded yet; TASK-651
owns the headless counterpart):

| Check | Expect |
|---|---|
| Keep personal host Codex and two container terminals talking across one genuine broker renewal | all three continue; same native identities and personal login; running container processes adopt the new access value, not just a new file or process |
| With the login due, run `/usr/local/libexec/agent-pm/agent-pm-broker refresh --container <container> --manifest <manifest>` for each run at once | exactly one native rotation, the waiter reuses it; no lock failure or lost login; a later due renewal succeeds |
| Record both | fixed/image/actual versions, generation digests, rotation count, per-stage timing, safe evidence of same-process adoption |
| Cut the network during a scheduled renewal, restore it before expiry | classified events, one-minute retries; same identity continues; no run login |
| Restart the supervisor, then interact | published deadline reread; renewal continues for the same history |
| A generation fails | the repair message in [Refresh and recovery](docker-codex.md#refresh-and-recovery); after the host command, resume keeps identity and history |
