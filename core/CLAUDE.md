# core

The portable core pack: `src/compose.py` compiles `team/` text (`guide.md` opens the prompt; what goes where: the root `CLAUDE.md` › Roles, tasks, rules) and `output/` into a prompt; `src/drive.py` runs it through a client (`src/clients/`) and a runner. It knows nothing of Linear or a fixed docs repo (`docs/adr/0001`). It is the Claude Code plugin `agent-pm` (`.claude-plugin/plugin.json`; `version` changes only when the user says), skills in `skills/`. Only `core/` is installed, copied per version to `~/.claude/plugins/cache/agent-pm/agent-pm/<version>/`: a skill or script references nothing outside it, a skill naming core's files by `${CLAUDE_SKILL_DIR}/../../…`, the only variable its `allowed-tools` rules get.

## Commands

```bash
python3 core/src/drive.py --role R --task T --input X --out O --workdir W [--runner tui …] --dry-run   # the agent run's argv, cwd and env; starts nothing
python3 core/src/drive.py --client skill --role R --task T   # the prompt /agent-pm:act-as follows
python3 core/src/tui_claude.py --help                        # host claude in tmux: start, send, read, show
```

## Rules

- `team/` and `output/` say what an agent run does and reports, client-neutrally; how a client starts a run and gets its outcome and progress back lives in that client's class.
- A task file may open with YAML frontmatter holding only `description: "<JSON string>"`: what the task does and when to use it, shown where role/tasks are listed (`skills/tmux/SKILL.md` › Role runs). `compose` strips it; no prompt sees it.
- A task marks where an agent run reports progress with a line `[agent-pm-progress:<name>] what to report`, under its own **Report progress** item (a step, or a bullet under the parent it belongs to), never inside another item; the client's `handover()` says how the run reports it.
- `src/repo.py` is stdlib-only and imports nothing from `src/`. `repo.checkout` is the one checkout path rule, for any caller. Symlinks in a `repo.py worktree` checkout follow `config.toml`'s `trusted_dirs` comment, set in the worktree's own config (which turns on the clone's `extensions.worktreeConfig`); an existing worktree whose setting disagrees is refused: remove it, run again.
- Runners: `drive.RUNNERS` (`--runner`, default `headless`); `tui` hosts the client's `Launch.interactive` through `src/tui_claude.py`. A new runner is a `drive.Runner` naming its `Launch` field in `starts`.
- `src/tui_claude.py` stays one file usable alone: tmux and the stdlib, Python 3.9 syntax, importing no core module and reading no config (`tui_claude_test.py` checks). Its tmux, `sh`, `pgrep` and `osascript` calls go through the injectable `proc`, so tests run none. A live check uses a private server: `tmux -S <socket>` on every call, cleanup included (a program calling bare `tmux`: `env -u TMUX TMUX_TMPDIR=<dir>`); inside a tmux pane `$TMUX` beats `TMUX_TMPDIR`, so `TMUX_TMPDIR=… tmux kill-server` kills the caller's server.
- Every printed attach command is `tui_claude.attach_command`'s (it adds `$TUI_ATTACH_PREFIX`); no command it runs carries the prefix. Every launcher of a child `claude` drops `tui_claude.PARENT_KEYS` (why: its comment).
- The built-in show (`open_pane`, when `show` is unset) stacks panes by opener (`opener()`): the first right of the opener's pane, each later one below the newest still-shown pane of the opener's other sessions; `--split`/`--split-from` split a chosen pane instead (a `show` command ignores both). `@opener` and `@pane`, the only placement state stored, are read back only after a fullmatch: any same-user process can set them. A `show` command runs with `/bin/sh -c` in the driver's context: it carries the trust of `commands`.
- `drive.py --detach` (needs `--events`) checks the run, then starts the same command as the driver in a new detached tmux session `<prefix>-<sid[:8]>-drive` (never a `TUI_SESSION`) on the caller's tmux server, and exits. However the driver ends (`tmux kill-session` included), it appends one `outcome` line to the events file (`skills/tmux/SKILL.md` › Role runs).

## An agent run's command

The claude client's (`src/clients/claude.py`), as `drive.py … --dry-run` prints it; env `[clients.claude.env]`:

```bash
claude -p '<prompt>' --session-id <sid> --model <tier's> --effort <effort's> <[clients.claude] flags> --setting-sources user \
  --output-format stream-json --verbose --settings '{"enabledPlugins": {"agent-pm@agent-pm": false}}' \
  --add-dir <dir>… --allowedTools 'Bash(<command>)'…
```

- A resume: `--resume` for `--session-id`, in the session's recorded cwd with its settings. `--add-dir`: the `read`/`write` dirs, and the workdir when the cwd is another. A trusted cwd: `--setting-sources user,project,local` and, with a `.mcp.json`, `--mcp-config <cwd>/.mcp.json`.
- No deny list: `--allowedTools` pre-approves the task's `commands` and `report.py`; auto mode and the user's settings decide the rest.
- tui: `claude '<prompt>' …` without `-p`, `--output-format` and `--verbose`, plus `--name <session>` (`<prefix>-<sid[:8]>`, `--prefix` default `<role>-<task>`) and a `Stop` hook in `--settings`; `--events FILE` gets its `done|blocked|dead` lines. `drive.py` prints `tmux attach -t '=<session>'` and shows the session (Rules: the built-in show); no pane to split → only the attach command, with the reason. In a container: `skills/tmux/SKILL.md` › In a container, or a `show` command asking a host-side watcher to attach.
- tui give-up: after the nudge, `STOP_LIMIT` more turn ends without an outcome (a progress report resets the count), or `WAIT_LIMIT` after the last progress report or the start without one → `drive.py` prints the attach command, leaves the session and exits 1. Until then an unanswered dialog waits in the pane: interactive `claude` asks there to trust a new folder (`-p` doesn't) and, without auto mode (Haiku, tier 4), for what isn't pre-approved; a dialog opened within about a second of a turn end gets the nudge's keys. After the outcome or a give-up the session is an unwatched agent with the run's pre-approvals and `drive.py`'s environment, nothing it does reported: end it with `tmux kill-session -t '=<session>'`.
- `claude` needs (nothing checks; a missing one fails the run): `-p`, `stream-json`, `--resume`, `--permission-mode auto`, `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`; for tui, interactive mode and `Stop` hooks in `--settings`; subagents, web search and fetch, file and shell tools; the Workflow tool and built-in `/deep-research` (deep research).

## Add a client

1. **Class**: `src/clients/<name>.py`, a `Client` subclass (`src/clients/base.py` documents each member); `keys` holds `"roles"` too for per-role/task entries.
2. **Launch** (`runs = True`): `launch()` maps `run.tier` and `run.effort` through its config (no mapping → `ConfigError`) and turns `access.dirs` and `access.commands` into its own permission flags (what it can't enforce stays a prompt request: `docs/adr/0003`). New session or resume from `params.sid` and `params.resume`; cwd `access.cwd`, its project settings only with `access.project` (both `drive.place`'s). Fill `transcript`, `resume` and, if it has an interactive command (the tui runner's), `interactive`.
3. **Outcome and progress** (`docs/adr/0006`): `handover()`, the Output › Return text, tells the run to report with `{{report}}` (`compose.report_command`, pre-approved): `progress <name> <text>`, and `outcome …` as its last action; `drive.validate` is the only outcome check. Have `interactive` run the report command plus `stop` at each turn end (claude: a `Stop` hook), plus `--pending KEY` when the hook's stdin JSON lists in-flight background work at top-level `KEY` (claude: `background_tasks`): tui nudges a run that stopped without an outcome, never one with pending work. Without `stop` lines tui never nudges; it gives up only after `WAIT_LIMIT`.
4. **Register** it in `REGISTRY` (`src/clients/__init__.py`); add `[clients.<name>]` to `config.toml` when `needs_config`.
5. **Test** in `src/tests/drive_test.py`: its argv and `interactive` for a role/task, resume, an unmapped tier, its events from a sample of its output.

Done when `drive.py --client <name> --dry-run` (and `--runner tui`, when it has `interactive`) prints the expected command for every role/task in `config.toml` and the suite passes.
