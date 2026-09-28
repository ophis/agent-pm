# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Unattended agent pipeline driven by a Linear board: a router runs the runnable projects in `pipeline.toml` (today Deep Research and Product Design), and promote hands issues between stages. README.md covers operations (schedule, Keychain, rescheduling); read it first.

## Commands

```bash
python3 -m unittest discover -s scripts/tests            # all tests (Python 3.11+); no network, Keychain or Claude
python3 -m unittest discover -s scripts/tests -k attempt # tests matching a name
python3 scripts/router.py --now --dry-run               # one tick: plan + usage probe, changes nothing
python3 scripts/router.py --plan --dry-run               # Linear side only
python3 scripts/promote.py --dry-run                     # what Handoff would do
python3 scripts/promote.py --now                         # promote now, skipping the 10-minute wait
```

A router tick without `--dry-run` claims an issue and starts claude; `--pick` claims too.

## Architecture

- **Router** (`scripts/router.py`, launchd `com.ophis.agent-pm.router`): hours → tmux lock (session `agent-pm`) → prune → Recover across runnable projects → plan → usage gate → resume or claim (priority, later stage first, oldest) → `start`/`resume` line → `launch.py`. Decisions go to `logs/router.log`.
- **Launcher** (`scripts/launch.py`): looks the project up in `pipeline.toml` (instructions, model, effort, add_dirs) and starts `claude -p` in tmux; output and the `end` line go to `logs/projects/<stage>.log` (named after the instructions file), the `end` line also to `runs.log`.
- **Runner vs stage**: `scripts/` decides what runs; `stages/<stage>.md` is what the agent does (filling a skeleton from `templates/` where it has one), passed by absolute path in the prompt (not a skill). Every stage file must define a resume rule; resumed sessions re-read it mid-flight.
- **State**: `logs/runs.log` (start/resume/end only) plus Linear issue history are the only memory between ticks. Recover derives liveness from transcript mtimes and the current SID from runs.log lines newer than the issue's last move to In Progress.
- **Shared module** (`scripts/pipeline.py`): Linear access, config loading, paths. Runs use cwd `WORK` and Recover reads `TRANSCRIPTS`, both derived there from the repo location (Claude keys transcripts by cwd), so moving the repo or `work/` orphans any resumable session. The LaunchAgent plists hold absolute paths; reinstall them after a move.
- **Handoff** (`scripts/promote.py`, its own LaunchAgent every 15 min, no LLM): Handoff issues → next stage per `pipeline.toml`. The child issue's id is derived from the source, target project and handoff time, which is what makes reruns idempotent.
- **Linear access**: `pipeline.py` calls the GraphQL API directly (key from Keychain, account `frank.agent.w`); agents use the `linear` skill (`~/.claude/skills/linear`). No Linear MCP.

## Gotchas

- A `claude -p` run can't touch anything outside its cwd `work/` unless it is added with `--add-dir`: it stops for a permission nobody can grant. Runs get `--add-dir <repo root> --add-dir ~/playground/private_docs`; any new path a stage needs goes in its `add_dirs`. Even so, auto mode held a `python3 ../scripts/...` call for approval in a test run.
- `test_launch.py` asserts exact prompt strings and `claude` flags; update it with any prompt or flag change.
- `pipeline.toml` keys projects (and `next`) by Linear project id, so renaming a project needs no change; the team is still matched by name.
- Board rules the code relies on: the agent never moves issues to Backlog; anything needing the user goes to In Review, assigned to the first `human_members` user (Recover only touches In Progress issues assigned to the agent); moving an issue back to Todo resets its attempt cap (4).
- `docs/` (design specs) and `logs/` are gitignored and exist only locally. `logs/runs.log` is runner state; never clean `logs/`. launchd jobs fail to start if `logs/` is missing.
- promote's Handoff query nests `attachments` without a `first` limit and lists at most 100 issues; fine at current volume, but if Linear starts rejecting it as too complex (limit 10000), add `attachments(first: N)`.
- Linear's issue lists can lag a just-made state change, and its history may omit moves made right after creation; promote re-reads the issue's state before acting.
- launchd runs router and promote with `/opt/homebrew/bin/python3`; macOS `/usr/bin/python3` is 3.9 and lacks `tomllib`.
