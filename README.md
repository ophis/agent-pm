# linear-research

Runs the Deep Research queue on the Linear board (team Frank's Agents, project Deep Research): picks the next Todo issue, runs `/deep-research` once, pushes the report to `~/playground/private_docs/Deep Research/` (GitHub `ophis/private_docs`), and links it from the issue.

Code lives in the skill `~/.claude/skills/linear-deep-research` (git repo `~/.claude`); this repo holds run state:

- `runs.log` — `start` / `resume` / `end` lines per run (issue, session ID) plus skips; the source for resuming and for the attempt cap. Pruned to 7 days each tick. Lost → nothing is resumed.
- `work/` — cwd of each `claude -p` run (gitignored).

Skill files:

- `SKILL.md` — the procedure Claude follows.
- `scripts/pick.py` — Linear side of a tick: `--plan` (Recover, then prints `resume …` or `new`), `--claim` (Pick + Claim with the attempt cap), `--gate resume|new` (usage thresholds), `--prune RUNS_LOG` (drop lines older than 7 days), `--dry-run` (no changes). Unknown flags exit 2. No mode = Recover + Pick + Claim (skill step 1); it mutates Linear, so explore with `--dry-run`. Needs the agent account's API key in the macOS Keychain:
  - Store: `security add-generic-password -a frank.agent.w -s linear-api-key -w` (prompts for the key).
  - Read: `pick.py` runs `security find-generic-password -a frank.agent.w -s linear-api-key -w` at startup and sends the key only as the `Authorization` header to `api.linear.app`; it is never printed or written to disk. Works under launchd because the LaunchAgent runs in the login session.
- `scripts/linear-research.sh` — one tick: hours check (01:00–06:59; `--now` skips it), tmux check, `pick.py --prune`, `pick.py --plan`, usage probe only when there is work, then resume an interrupted In Progress issue (`--resume <sid>`, highest priority first) or claim a new issue. Both need the probe not rejected, 5-hour usage < 90% and weekly windows not full (`MAX_5H` in `pick.py`). `--dry-run` changes nothing and starts no run.
  - Scheduled hourly 01:00–06:00 by `~/Library/LaunchAgents/com.ophis.linear-research.plist`; source: `scripts/com.ophis.linear-research.plist` in the skill. launchd output goes to `~/Library/Logs/linear-research.log`.
  - Watch: `tmux attach -t linear-research`. Inspect a run: `cd work && claude --resume <session-id>`.
- `scripts/tests/` — `python3 -m unittest discover -s scripts/tests` (no network, Keychain or Claude).
- Design: `docs/specs/2026-09-27-auto-resume-design.md`, amended by `docs/specs/2026-09-27-resume-candidate-fix-design.md`.
- `review/` — unused draft (reuses one interactive tmux session); not wired into anything.

Notes:
- `linear-research.sh` sets `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`; `claude -p` otherwise kills the research workflow after 10 idle minutes.
- One research run costs roughly 40% of the 5-hour window at `--effort xhigh`.

## Rescheduling

Edit `StartCalendarInterval` in the skill's `scripts/com.ophis.linear-research.plist`, copy it to `~/Library/LaunchAgents/`, then reload (launchd reads the plist only at load). Keep the hours window in `linear-research.sh` in sync.

```bash
P=~/Library/LaunchAgents/com.ophis.linear-research.plist
plutil -lint $P
launchctl bootout gui/$(id -u)/com.ophis.linear-research
launchctl bootstrap gui/$(id -u) $P
launchctl print gui/$(id -u)/com.ophis.linear-research | grep -A3 descriptor   # confirm the new time
```

Stop the schedule: `launchctl bootout gui/$(id -u)/com.ophis.linear-research` (plus delete the plist, or it loads again at next login).
