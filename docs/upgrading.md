# Upgrading

Before upgrading: [Upgrading steps](#upgrading-steps). From v1: [Switching from v1](#switching-from-v1), then the [Smoke checklist](#smoke-checklist).

## Upgrading steps

Nothing migrates automatically. Do each step that applies, once, by hand or through an agent.

| If you have | Do |
|---|---|
| running agent-pm sessions | Close them first. |
| running Docker Host sessions | Close them before upgrading: their view sessions' old tag and their records' `hidden` field are no longer read. |
| `workers_per_column` or `grid_retile` in `[session.hosts.tmux]` | Move them to `[session.viewer]`; left there, the tmux Host refuses them with `unknown key`. |
| running Docker Host sessions whose `session.json` `host_hook.module` is `agent_pm.session.hosts.tmux.docker_hook` | After upgrading, close and reopen each: that module moved to `agent_pm.session.hosts.docker.hook`, so their host hook no longer loads. |
| a flat `config.local.toml` | Move top-level values under `[session]`, `[delegate]` or `[linear_orchestrator]`, per the layer's packaged `config.toml`, and prefix tables with that section: `[roles.engineer]` becomes `[delegate.roles.engineer]` (run) or `[linear_orchestrator.roles.engineer]` (board). `show` goes under `[delegate]` or `[delegate.roles.<role>]`. `core-new.local.toml` goes under `delegate`, `linear-new.local.toml` under `linear_orchestrator`. |
| a `runtime` key or `--runtime` | Rename it `client` everywhere. Records written with `runtime` are not read. |
| `[session.adapters.<name>]` tables, or an `implementation` naming `agent_pm.session.adapters` or an `…Adapter` class | Rename them `[session.clients.<name>]`, `agent_pm.session.clients` and `…Client` (`ClaudeClient`, `CodexClient`); an extension's `Adapter` base is now `Client`; a Host's `HostProcess` is `Process`, `Terminal` is `InteractiveProcess` (its `send` / `press` in `Interactive`), `Displayable` is gone (a Host names an attach target the Viewer shows) and `NotDisplayable` is `NotShowable`. |
| `~/.agent-pm/core-new-sessions/codex` | Close Codex TUI sessions, then `mkdir -m 700 -p ~/.agent-pm/sessions && mv ~/.agent-pm/core-new-sessions/codex ~/.agent-pm/sessions/ && rmdir ~/.agent-pm/core-new-sessions`. |
| a v1 manager roster: `~/.agent-pm/managers/<name>/roster.json` with `"version": 1`, which fails `agent-pm-crew status` and `attach` with `want roster fields` | Back up `~/.agent-pm/managers/`, then move its contents to the Trash. The next `agent-pm-crew attach` starts an empty roster. |
| `default_task` or `tasks` in a role table | Drop `default_task`. Move each `<prefix>.roles.<role>.tasks.<task>` table's keys to `<prefix>.roles.<role>`, for the prefixes `delegate`, `delegate.clients.<name>` and `linear_orchestrator.core`. |
| `[session.clients.codex]` `sandbox`, `shell_network` | Express them in `[delegate.policy.codex]` `flags`. |
| a local `flags` list in `[session.clients.claude]` or `[session.clients.codex]` | A local list replaces the packaged one whole: add its agent-pm entry, Claude `"--settings", '{"enabledPlugins": {"agent-pm@agent-pm": false}}'`, Codex `"-c", "plugins.agent-pm@agent-pm.enabled=false"`. |
| `--permission-mode`, `--strict-mcp-config` in `[session.clients.claude]` | Move them to `[delegate.policy.claude]`; keep there only flags every session needs. |
| `runs.jsonl` start, resume or limit rows without `delegate_kind` | Before deploying, add `"delegate_kind": "harness"` to each, changing nothing else. |
| an issue In Progress across the trigger-only router or `runs.jsonl` deploys | Deploy with nothing In Progress. |
| Claude runs started before `agent-pm-session client-resume claude` | Resume them by hand: their `Run <sid>` command has no saved launch to replay. |
| a `~/.agent-pm/bin/ctmux` that is a copy of ctmux, not a launcher (its sessions exit at once outside Claude Code), or launchers installed before ctmux moved to `manage` (`ctmux` fails with `no installed agent-pm plugin has manager/skills/tmux/scripts/ctmux`) | Run the plugin's ctmux once: `<plugin>/manager/skills/manage/scripts/ctmux --install`, `<plugin>` being `~/.claude/plugins/cache/agent-pm/agent-pm/<version>` or `~/.codex/plugins/cache/agent-pm/agent-pm/<version>`. |
| old logs in `<work_dir>/logs/` | `rm -rf ~/.agent-pm/logs/runs.log ~/.agent-pm/logs/router.log ~/.agent-pm/logs/promote.log ~/.agent-pm/logs/projects ~/.agent-pm/logs/tui` |
| `src/` or `run.jsonl` directly in a `<work_dir>/work/<ID>/` or `~/.agent-pm/adhoc/*/`, or `~/.agent-pm/delegates/` | [Workdir split](#workdir-split). |
| scripts or notes calling `agent-pm-crew attach --after` or `--gen` | Switch to `agent-pm-crew ack --through <line> --gen <gen>`; reading events no longer marks them handled. |
| `[linear_orchestrator.option_labels]` (the `visual` key) in `~/.agent-pm/config.local.toml` | Delete that table: research reports now carry figures on their own, so the Visual label does nothing; left there, the router refuses it with `unknown key 'option_labels' in [linear_orchestrator]`. |

### Workdir split

A run's workdir, `<work_dir>/work/<ID>/` or an ad-hoc `~/.agent-pm/adhoc/<date>-<role>-<name>/`, now holds `agent/` and `run/`. Nothing reads the old layout, and no run started before the move resumes (its cwd moved). The move keeps each issue's checkouts, which prune then finds, and its records; Codex recovery state stays as a record.

1. With no live run and nothing In Progress, move each workdir's files (a finished ad-hoc folder can be deleted instead: [State root](state-root.md)):
   ```bash
   for w in <work_dir>/work/*/ ~/.agent-pm/adhoc/*/; do
     [ -d "$w" ] && [ ! -d "$w/run" ] || continue
     mkdir "$w/agent" "$w/run"
     for f in src publish tmp prompt.md; do [ -e "$w/$f" ] && mv "$w/$f" "$w/agent/"; done
     for f in run.jsonl out.md delegate.json delegate.lock; do [ -e "$w/$f" ] && mv "$w/$f" "$w/run/"; done
   done
   ```
2. Reconnect each moved worktree (a checkout whose `.git` is a file) to its `[local_clones]` clone:
   ```bash
   for g in <work_dir>/work/*/agent/src/*/*/.git ~/.agent-pm/adhoc/*/agent/src/*/*/.git; do [ -f "$g" ] && git -C "${g%/.git}" worktree repair; done
   ```
3. `rm -rf ~/.agent-pm/delegates`

## Switching from v1

v1 is the `v1-runtime` checkout: `core/`, `orchestrator/`, and overrides in `~/.agent-pm/core.local.toml` and `~/.agent-pm/orchestrator.local.toml`. Nothing reads v1's files or state. Do these in order, in the checkout the launchd jobs ran:

1. **Drain the board.** Wait until nothing is In Progress and `tmux ls | grep '^agent-pm-'` prints nothing. v1 runs can't be resumed or written back.
2. **Stop v1.** Unload its jobs and kill every agent-pm tmux session (v1 tui sessions `<role>-<ID>-<8 hex>` and workers):
   ```bash
   for job in router promote; do launchctl bootout gui/$(id -u)/com.ophis.agent-pm.$job; done
   tmux kill-session -t '=<name>'   # each agent-pm session tmux ls lists
   ```
3. **Switch to main:** `git switch main && git pull --ff-only`. It fails while another worktree of the repo has `main` checked out: run `git switch --detach` there first. Then move `core/` and `orchestrator/` to the Trash if they remain: untracked, they hold only `__pycache__`.
4. **Reinstall the plugin.** v1 was also 0.4.0, so `claude plugin update` keeps v1's files. Reinstall it, then run `/reload-plugins` in each open Claude Code session:
   ```bash
   claude plugin uninstall agent-pm@agent-pm
   claude plugin marketplace update agent-pm
   claude plugin install agent-pm@agent-pm
   ```
   `~/.claude/plugins/cache/agent-pm/agent-pm/<version>/` must hold `delegate/` and `manager/`, not v1's `skills/`, `src/` and `team/`. Codex: [Install](../README.md#install), refreshing the added marketplace. Then `ctmux --install`.
5. **Move the overrides** into `~/.agent-pm/config.local.toml` by the table below, values unchanged, then delete the two v1 files. `bin/agent-pm-linear router --dry-run` must print no `config-error`.
6. **Do the [Upgrading](#upgrading-steps) rows** that apply. Rotate any real credential ever passed in a Claude run's env: earlier resume commands printed it into `run.jsonl` and session comments.
7. **Run the [Smoke checklist](#smoke-checklist).**
8. **Install launchd** by [Setup](board.md#setup) step 6 once the smoke checklist passes. Its first runs clear v1's leftovers: promote's prune deletes the checkouts and temp files `prune --dry-run` listed, and the router's Recover moves an issue left In Progress by v1 back to Todo (`recover`, `"reason": "no session"`).

`show` changed meaning: v1's `""` opened nothing. Now `""` opens a pane and `"none"` opens nothing until `show`.

| v1 | `config.local.toml` |
|---|---|
| `core.local.toml` `tier`, `effort`, `language`, `cwd`, `show`, `users`, `trusted_dirs`, `team_dirs` | `[delegate]` |
| `core.local.toml` `status_line` | `[session.hosts.tmux]` |
| `core.local.toml` `workers_per_column`, `grid_retile` | `[session.viewer]` |
| `core.local.toml` `[output]`, `[roles.<role>]` | `[delegate.output]`, `[delegate.roles.<role>]` |
| `core.local.toml` `[clients.claude]` `flags` | `--permission-mode`, `--strict-mcp-config` to `[delegate.policy.claude]`, the rest to `[session.clients.claude]` |
| `core.local.toml` `[clients.claude]` `allow`, `env`, `tiers`, `efforts` | `[session.clients.claude]`, same names |
| `core.local.toml` `[clients.codex]` `tiers`, `efforts` | `[session.clients.codex]`, same names |
| `core.local.toml` `[clients.codex]` `shell_network` | a `[delegate.policy.codex]` flag ([Client policy](sessions.md#client-policy)) |
| `core.local.toml` `[clients.skill.…]`, `[clients.<name>]` `implementation` | drop |
| `orchestrator.local.toml` top-level keys | `[linear_orchestrator]` |
| `orchestrator.local.toml` `[states]`, `[task_labels]`, `[project_repos]`, `[local_clones]`, `[roles.<role>]`, `[core]` | the same tables under `linear_orchestrator.`; drop a `gate` copied into `[core]` |

## Smoke checklist

Run it right after switching to `main`, in order, from the checkout root inside tmux; stop at the first failure. Steps 1–6 change nothing in Linear. Steps 7–9 need two throwaway Todo issues in a test project: `<H>` for the engineer (Light Build, a `Repo: <owner>/<scratch repo>` line and a one-line change) and `<T>` for the researcher (Light Research, a one-line question).

| # | Check | Run | Passes when |
|---|---|---|---|
| 1 | Commands | each `bin/` command's `--help`, then `agent-pm-session ls`, `agent-pm-delegate run --list`, `agent-pm-crew status --manager <name>` | all exit 0; `run --list` shows researcher, pm, engineer and dummy-tester |
| 2 | Config validation | the block below | each command fails with `unknown key 'smoke_bad_key' in [<section>]` before any Linear call |
| 3 | Claude Code plugin | `/agent-pm:act-as dummy-tester:echo smoke` in a throwaway session | it echoes `smoke`; `/agent-pm:manage` loads |
| 4 | Codex plugin | `codex --version`; in a Codex session, pick `/agent-pm:manage` and ask it to run `agent-pm-session ls` | 0.156.0+; Codex runs `bin/agent-pm-session` by absolute path, with no literal `${CLAUDE_SKILL_DIR}` |
| 5 | Dry runs | `bin/agent-pm-linear` `router --dry-run`, `router --issue <H> --tui --events "$(mktemp)" --dry-run`, `promote --dry-run`, `prune --dry-run` | each prints the plan you expect and exits 0; no issue moves, no tmux session starts |
| 6 | Brake | `bin/agent-pm-linear router --brake researcher` | exit 0 or 1, not 2 |
| 7 | Headless run | `bin/agent-pm-linear router --issue <H>` | `<H>` goes In Progress with its start and `Run <sid>` comments and an `engineer-<H>-<sid8>-drive` session; it ends with a PR on the scratch repo linked in a comment, `<H>` In Review, the session gone |
| 8 | Attended run | `bin/agent-pm-linear router --issue <T> --tui --events "$(mktemp)"` | as step 7, with a `researcher-<T>-<sid8>` TUI; the report is published, linked, and `<T>` is In Review |
| 9 | Router lock | the block below, with `<H>` In Review | `router.py: another router is running; try again`, exit 2 |

```bash
# step 2: before each command, put `smoke_bad_key = 1` under only that section of the copy
smoke=$(mktemp -d) && mkdir "$smoke/.agent-pm" && cp ~/.agent-pm/config.local.toml "$smoke/.agent-pm/"
HOME=$smoke bin/agent-pm-session ls                 # [session]
HOME=$smoke bin/agent-pm-delegate run --list        # [delegate]
HOME=$smoke bin/agent-pm-linear router --dry-run    # [linear_orchestrator]
rm -r "${smoke:?}"

# step 9: hold <work_dir>/logs/router.lock for 60 s, then run the router
python3 -c 'import fcntl, os, time; f = open(os.path.expanduser("~/.agent-pm/logs/router.lock"), "a"); fcntl.flock(f, fcntl.LOCK_EX); time.sleep(60)' &
bin/agent-pm-linear router --issue <H>
```

On a failure, read `<work_dir>/logs/orchestrator.jsonl` and `<work_dir>/work/<ID>/run/run.jsonl`. End a stuck run with `bin/agent-pm-linear close <ID>`. A check that passes when it should fail (steps 2, 4, 9) is a bug: report it.
