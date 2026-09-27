# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Unattended agent pipeline driven by a Linear board. Today it runs one stage, Deep Research; it is meant to grow into a multi-project router with Handoff between stages (Linear TASK-20). README.md covers operations (schedule, Keychain, rescheduling); read it first.

## Commands

```bash
python3 -m unittest discover -s scripts/tests            # all tests (Python 3.11+); no network, Keychain or Claude
python3 -m unittest discover -s scripts/tests -k attempt # tests matching a name
scripts/linear-research.sh --now --dry-run               # one tick: plan + usage probe, changes nothing
python3 scripts/pick.py --dry-run --plan                 # Linear side only
python3 scripts/promote.py --dry-run                     # what Handoff would do
```

`pick.py` with no mode claims an issue in Linear; always pass `--dry-run` when exploring.

## Architecture

- **Tick** (`scripts/linear-research.sh`, fired by launchd): hours → tmux lock (session `linear-research`) → `pick.py --prune` → `pick.py --plan` → usage gate → `claude -p` in tmux, either `--resume <sid>` of an interrupted run or `--session-id` of a newly claimed issue. The tmux command appends the `end` line to `runs.log`.
- **Runner vs stage**: `scripts/` decides what runs; `stages/deep-research.md` is what the agent does, passed by absolute path in the prompt (not a skill). Resumed sessions re-read it mid-flight, so its resume rule applies to runs already in progress.
- **State**: `runs.log` (start/resume/end per session) plus Linear issue history are the only memory between ticks. Recover in `pick.py` derives liveness from transcript mtimes and the current SID from runs.log lines newer than the issue's last move to In Progress.
- **Paths derive from the repo location** (`ROOT` in `pick.py`, `STATE` in the runner). Runs use cwd `work/`, and Claude keys transcripts by cwd (`~/.claude/projects/<cwd with / and . → ->`), so moving the repo or `work/` orphans any resumable session. The LaunchAgent plist holds an absolute path; reinstall it after a move.
- **Handoff** (`scripts/promote.py`, its own LaunchAgent every 15 min, no LLM): Handoff issues → next stage per `pipeline.toml`. The child issue's id is derived from the source, target project and handoff time, which is what makes reruns idempotent.
- **Linear access**: `pick.py` calls the GraphQL API directly (key from Keychain, account `frank.agent.w`); agents use the `linear` skill (`~/.claude/skills/linear`). No Linear MCP.

## Gotchas

- A `claude -p` run can't touch anything outside its cwd `work/` unless it is added with `--add-dir`: it stops for a permission nobody can grant. Runs get `--add-dir <repo root> --add-dir ~/playground/private_docs`; any new path a stage needs must be added in `linear-research.sh`. Even so, auto mode held `python3 ../scripts/pick.py` for approval in a test run.
- `test_dispatcher.py` runs a copy of `linear-research.sh` under a temp `$HOME` and asserts exact prompt strings and `claude` flags; update it with any prompt or flag change.
- Board rules the code relies on: the agent never moves issues to Backlog; anything needing the user goes to In Review; moving an issue back to Todo resets its attempt cap (4).
- `docs/` (design specs) and `logs/` are gitignored and exist only locally. `logs/runs.log` is runner state; never clean `logs/`. launchd jobs fail to start if `logs/` is missing.
- promote's Handoff query nests `attachments` without a `first` limit and lists at most 100 issues; fine at current volume, but if Linear starts rejecting it as too complex (limit 10000), add `attachments(first: N)`.
- Linear's issue lists can lag a just-made state change, and its history may omit moves made right after creation; promote re-reads the issue's state before acting.
- launchd runs promote with `/opt/homebrew/bin/python3`; macOS `/usr/bin/python3` is 3.9 and lacks `tomllib`.
