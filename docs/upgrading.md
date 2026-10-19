# Upgrading

This covers upgrades from v0.6.20 on; upgrades from before v0.6.20 are not covered. Before upgrading, do the
[Upgrading steps](#upgrading-steps) that apply.

## Upgrading steps

Nothing migrates automatically. Do each row that applies, once, by hand or through an agent.

| If you have | Do |
|---|---|
| running agent-pm sessions | Close them first, Docker Host ones before you reinstall the broker too: a reinstalled broker cannot read, so cannot revoke, the old one's GitHub tokens. After a reinstall, delete leftover `agent-pm-tokens` and `agent-pm-revocations` items in Keychain Access (their tokens expire within an hour), then [log in](docker-host.md#log-in) again. |
| `[linear_orchestrator.selection]` in `~/.agent-pm/config.local.toml` (model selection, removed) | Delete it with its `selector`, `resolvers` and `translate` keys. Left there, `agent-pm-linear` refuses with `unknown key 'selection' in [linear_orchestrator]`; only `writeback` reports and ignores it. |
| `[linear_orchestrator.core.roles.choice-translator]` or `[delegate.roles.choice-translator]` in `~/.agent-pm/config.local.toml` | Delete them: the `choice-translator` role and its `translate` task are gone. Left there, the orchestrator refuses with `unknown role 'choice-translator' in [overlay.roles]` or `roles.choice-translator: no charter roles/choice-translator.md in core or team_dirs`. |
| `[selector]` or `[selector.jev]` in `~/.agent-pm/config.local.toml`, Keychain item `typesafe-jev` | Delete them: nothing reads them any more (the config table is ignored). |
| `runs.jsonl` start rows with `tier` and `effort`, written while `[linear_orchestrator.selection] resolvers` was non-empty | Nothing to do: readers ignore those fields. Resuming such a run uses its task's `[task_tiers]` tier and the role's configured effort, not the recorded choice. |
| board OpenCode runs relying on v0.6.32's packaged `[client_max_runs] opencode = 1` | Add `[linear_orchestrator.client_max_runs]` `opencode = 1` to `~/.agent-pm/config.local.toml`: the packaged config no longer limits any client ([Run limits](board.md#run-limits)). |
| role runs begun on v0.6.30 or earlier | Let them finish first: an outcome's `summary` now holds at most 300 characters (4000 before), so `agent-pm-linear writeback` refuses a longer one with exit 2, and the run's Linear comment waits for Recover. |
| Docker Host workspace volumes `agent-pm-ws-*` whose worker gets `Permission denied` writing its cwd (root-owned roots, v0.6.23 or earlier) | Close the session, then `/usr/local/libexec/agent-pm/agent-pm-broker workspace-rm --volume <volume>` (it holds no work); the next start recreates it agent-owned. |
| `[session.hosts.docker.github] app`, broker protocol 12 or earlier | After upgrading plugin and broker, run `/usr/local/libexec/agent-pm/agent-pm-broker login github agent-pm-bot` once: until it saves the bot user ID, a start fails with `login_required`. |
| `[session.clients.opencode.providers.<name>]` tables and Docker OpenCode, broker protocol 14 or earlier | After upgrading plugin and broker, run `/usr/local/libexec/agent-pm/agent-pm-broker login opencode <name>` for each: until then a Docker OpenCode start fails with `login_required`. |
| Docker Codex installed with broker protocol 11 or earlier | [Upgrade existing installations](docker-codex.md#upgrade-existing-installations). |
| scripts calling `/opt/agent-pm/bin/with-secrets` by hand | Add `--client <client>` (`claude`, `codex` or `opencode`); without it the command refuses. |
| keychain items `agent-pm-engineer` or `agent-pm-docs` (GitHub App private keys) | The broker serves only the App `agent-pm-bot` (App ID 5352992): store its key as [Log in](docker-host.md#log-in) says (service `agent-pm-bot`, account `5352992`), run `/usr/local/libexec/agent-pm/agent-pm-broker login github agent-pm-bot`, and delete the two old items. |
| `mount_roots` in `[session.hosts.docker]` | Delete it (workspaces are [volumes](docker-host.md#workspace-volumes)); left there, the Docker Host refuses it with `unknown key`. |
| `[session.hosts] default = "docker"` with unattended board runs | They now run in containers ([Role runs](docker-host.md#role-runs)): set `[delegate.delegates] harness_host` to another Host and drop host `read`/`write` dirs from their roles. |
| `default_task` or `tasks` in a role table | Drop `default_task`. Move each `<prefix>.roles.<role>.tasks.<task>` table's keys to `<prefix>.roles.<role>`, for the prefixes `delegate`, `delegate.clients.<name>` and `linear_orchestrator.core`. |
| `[session.clients.codex]` `sandbox`, `shell_network` | Express them in `[session.clients.codex.modes]` `auto` ([Client policy](sessions.md#client-policy)). |
| a local `flags` list in `[session.clients.claude]` or `[session.clients.codex]` | A local list replaces the packaged one whole: add its agent-pm entry, Claude `"--settings", '{"enabledPlugins": {"agent-pm@agent-pm": false}}'`, Codex `"-c", "plugins.agent-pm@agent-pm.enabled=false"`, and for Codex the browser, computer use, Apps and `tool_suggest` entries of its packaged list ([Session settings](sessions.md#session-settings)). |
| permission flags in a local `flags` list: `--permission-mode`, `--dangerously-skip-permissions` (`[session.clients.claude]`, `[delegate.policy.claude]`), `-s`, `approval_policy`, `approvals_reviewer`, `--dangerously-bypass-approvals-and-sandbox` (`[session.clients.codex]`, `[delegate.policy.codex]`) | Remove them: permission flags live only in `[session.clients.<client>.modes]` `auto` and `yolo` ([Client policy](sessions.md#client-policy)); in `flags` they reach every Host, the Docker Host's yolo sessions too. Change a mode there (a local list replaces that mode whole), e.g. `[session.clients.codex.modes] auto = [...]`. Keep `--strict-mcp-config` in `[delegate.policy.claude]`. |
| scripts or prompts passing `-- --permission-mode auto` to Claude workers | Drop it: auto mode adds it, and on the Docker Host it would sit beside yolo's. |
| Codex sessions started before a resume read the current `[session.clients.codex] flags` | Their recovery state also holds that list as it was at their start, which a resume repeats after the current one: an entry since dropped from a local list stays in force for them. Close and start them again. |
| `--add-dir` in a local `[session.clients.codex] flags` list | Remove it and grant the directory through the role's `write` dirs: left there, every Codex launch refuses with `--add-dir: [clients.codex] flags grant no directory`. |
| Codex sessions started before this upgrade | Their recovery state keeps the old `-s` and approval flags, which a resume adds to its mode's (on the Docker Host `--dangerously-bypass-approvals-and-sandbox` overrides them): close and start them again to drop them. |
