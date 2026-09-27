# linear-research

Runs the Deep Research queue on the Linear board (team Frank's Agents, project Deep Research): picks the next Todo issue, runs `/deep-research` once, pushes the report to `~/playground/private_docs/Deep Research/` (GitHub `ophis/private_docs`), and links it from the issue.

Code lives in the skill `~/.claude/skills/linear-deep-research` (git repo `~/.claude`); this repo holds run state:

- `runs.log` — one line per run with the session ID and transcript path.
- `work/` — cwd of each `claude -p` run (gitignored).

Skill files:

- `SKILL.md` — the procedure Claude follows.
- `scripts/pick.py` — recovers issues left by dead runs, then claims the next Todo issue via the Linear API. Needs the agent account's API key in the macOS Keychain:
  - Store: `security add-generic-password -a frank.agent.w -s linear-api-key -w` (prompts for the key).
  - Read: `pick.py` runs `security find-generic-password -a frank.agent.w -s linear-api-key -w` at startup and sends the key only as the `Authorization` header to `api.linear.app`; it is never printed or written to disk. Works under launchd because the LaunchAgent runs in the login session.
- `scripts/linear-research.sh` — headless runner: runs `pick.py`, then `claude -p` on the claimed issue in a detached tmux session `linear-research`, one run at a time, logs to `runs.log`. Empty queue → no Claude session.
  - `linear-research.sh` — real run. `linear-research.sh --dry-run` — reports what `pick.py` would recover and pick; changes nothing, starts no Claude.
  - Scheduled daily at 00:00 and 05:00 by the LaunchAgent `~/Library/LaunchAgents/com.ophis.linear-research.plist` (not in git); launchd output goes to `~/Library/Logs/linear-research.log`.
  - Watch: `tmux attach -t linear-research`. Inspect a finished run: `claude --resume <session-id>`.
- `review/` — unused draft (reuses one interactive tmux session); not wired into anything.

Notes:
- `linear-research.sh` sets `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`; `claude -p` otherwise kills the research workflow after 10 idle minutes.
- One research run costs roughly 40% of the 5-hour window at `--effort xhigh`.

## Rescheduling

Edit the `Hour`/`Minute` entries under `StartCalendarInterval` (an array, one dict per time) in the plist, then reload (launchd reads the plist only at load):

```bash
P=~/Library/LaunchAgents/com.ophis.linear-research.plist
plutil -lint $P
launchctl bootout gui/$(id -u)/com.ophis.linear-research
launchctl bootstrap gui/$(id -u) $P
launchctl print gui/$(id -u)/com.ophis.linear-research | grep -A3 descriptor   # confirm the new time
```

Stop the schedule: `launchctl bootout gui/$(id -u)/com.ophis.linear-research` (plus delete the plist, or it loads again at next login).
