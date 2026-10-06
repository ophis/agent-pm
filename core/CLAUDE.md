# core

The portable core pack: `team/` text (guide, principles, roles, tasks, methods, templates) and `output/` (how an agent run hands back) compiled by `src/compose.py` and run by `src/drive.py` through a client in `src/clients/` and a runner. It knows nothing of Linear or a fixed docs repo (`docs/adr/0001`).

## Commands

```bash
python3 -m unittest discover -s core/src/tests -p "*_test.py"   # no network, Claude, tmux or osascript
python3 core/src/drive.py --role R --task T --input X --out O --workdir W [--runner tui [--split right|below] [--beside SESSION]] --dry-run   # the command an agent run gets
python3 core/src/drive.py --role R --task T --input X --out O --workdir W --runner tui   # a live agent run in tmux session <role>-<task>-<sid[:8]>
python3 core/src/tui.py --help                                   # host any command in tmux: start, send, read, show
core/regen_skills.sh                                             # after editing team/, output/, config/ or src/repo.py
```

Commit the regenerated `core/skills/` with the change that caused it.

## Rules

- `team/` and `output/` say what an agent run does and what it reports, client-neutrally. How a client starts a run and how the run's outcome and progress come back live in that client's class.
- A task marks where an agent run reports progress with a line `[agent-pm-progress:<name>] what to report`, under its own **Report progress** item (a step, or a bullet under the parent it belongs to), never inside another item; how the run reports it is the client's `handover()` (Claude: `report.py progress <name> <report>`; skill: before the next tool call, a text message holding only that line, the report after the mark). A done or failed run whose task marks `start` but never reported it gets a `missing` event (`drive.start`).
- Config is layered (`compose.load_run`): `config/config.toml`'s neutral part (top level and `roles`) is valid on its own; each `[clients.<name>]` table has the same layout and its run keys replace the neutral ones.
- `src/repo.py` is stdlib-only and imports nothing from `src/`: the skill client copies it into each skill, and `team/methods/` into a skill that names it.
- A runner (`drive.RUNNERS`, chosen with `--runner`, default `headless`) starts the client's command and decides when the agent run is done; the driver loop is the same for every runner. `headless` runs `Launch.argv` on a pipe until it exits; `tui` runs `Launch.interactive` in a detached tmux session through `src/tui.py`. A new runner is a `drive.Runner` naming its `Launch` field in `starts`; a client without that command is a `ConfigError` before anything starts.
- `src/tui.py` is a generic tmux host usable alone: keep it one file on tmux and the stdlib, in Python 3.9 syntax (`python3` may be macOS's), importing no core module and reading no config (`tui_test.py` checks). Its tmux, `sh`, `pgrep` and `osascript` calls go through the injectable `proc`, so tests run none. `start` runs its show (`show()`; the tui runner passes the `show` run key), resolved as `config/config.toml`'s header says. `drive.start(…, layout=drive.Layout(split, beside))` sets the tui runner's default show split and anchor session (`None` → `Layout()`; a layout with headless, a bad split or a bad beside → `ConfigError` before anything starts; `drive.py --split/--beside`); a `show` template ignores it. `drive.tui_session(role, task, sid)` names the TUI session (`drive.TUI_SESSION` fullmatches such names). `tui.own_session()` is the tmux session of the caller's pane, None outside tmux; `tui.anchor()` is the pane the show splits (`beside`'s client, else the caller's pane when a client shows its session, else `$ITERM_SESSION_ID`'s), else `TuiError`; `tui.open_pane()` splits it in iTerm2 when an iTerm2 pane shows it, else with `tmux split-window`. `show` is a shell command run with `/bin/sh -c` in the driver's context: it carries the trust of `commands`.

## Add a client

1. **Class.** `src/clients/<name>.py`, a `Client` subclass (it is also the prompt's `compose.Vehicle`) setting:
   - `keys`: its own config keys (`"roles"` too for per-role/task entries);
   - `needs_config`: whether it reads `[clients.<name>]` in `config/config.toml`;
   - `runs`: `True` → `launch()` starts an agent run; `False` → `export()` writes files (as `skill` does);
   - `scripts_path(root)` and `methods_path(root)`, only when prompts must name `core/src/` or `core/team/methods/` other than by their absolute path (skill: `${CLAUDE_SKILL_DIR}/scripts`, `${CLAUDE_SKILL_DIR}/methods`).
2. **Launch** (`runs = True`): `launch(prompt, run, *, params, access) -> Launch(argv, env, cwd, interactive=…)`.
   - Map `run.tier` and `run.effort` through its config; a value with no mapping is a `ConfigError`.
   - Turn `access.dirs` and `access.commands` into its own permission flags; whatever it can't enforce stays a prompt request (`docs/adr/0003`).
   - New session vs resume from `params.sid` and `params.resume`; `cwd` = `params.workdir`.
   - `interactive`: the same agent run as the client's interactive command, for the tui runner; leave it empty when the client has none (no tui).
3. **Outcome and progress** (`docs/adr/0006`): override `handover()`, the Output › Return text telling the agent run to report both with `{{report}}` (filled with `compose.report_command`: `src/report.py --to <workdir>/.report.jsonl`, pre-approved for every run): `progress <name> <text>`, and `outcome --status … --title … --summary …` as its last action. `drive.start` creates that channel before launch, tails it during the run (only lines appended after it began) and once more after the runner ends, keeps the last outcome and validates it (`drive.validate`, the only check). `events(lines)` turns a headless run's stdout into `Event("text")`. Have `interactive` append `stop` to the channel at each turn end (`compose.report_command` + ` stop`; claude: a `Stop` hook in `--settings`): `stop` lines go to the runner, never the sinks, and tui nudges a run that stopped without an outcome. Add `--pending KEY` when the hook's stdin JSON holds the run's in-flight background work as a list at top-level `KEY` (claude: `background_tasks`; another input shape needs another option in `report.py`): the line then carries `pending`, and tui neither nudges nor counts a stop with pending work. Without `stop` lines tui never nudges a run; it gives up only after `drive.WAIT_LIMIT` with no progress report or outcome.
4. **Register** it in `REGISTRY` (`src/clients/__init__.py`); add `[clients.<name>]` to `config/config.toml` when `needs_config`.
5. **Test** in `src/tests/drive_test.py`: its argv and `interactive` for a role/task, resume, an unmapped tier, its events from a sample of its output.

Done when `drive.py --client <name> --dry-run` (and `--runner tui`, when it has `interactive`) prints the expected command for every role/task in `config/config.toml` and the suite passes.
