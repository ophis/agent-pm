# agent-pm

Runs agent stages from the Linear board (team Frank's Agents), one project per stage. Runnable today: Deep Research (picks the next Todo issue, runs `/deep-research` once, pushes the report to `~/playground/private_docs/Deep Research/` (GitHub `ophis/private_docs`), links it from the issue). Promote hands reviewed issues to the next stage.

This repo holds the code and the run state:

- `logs/` (gitignored) — never clean it out: `runs.log` is also runner state.
  - `runs.log` — `start` / `resume` / `end` lines per run (issue, session ID); the source for resuming and for the attempt cap. Pruned to 7 days each tick. Lost → nothing is resumed.
  - `router.log` — each tick's decisions. `projects/<project>.log` — each run's launch line, claude output and end line. `promote.log` — promote's output (one line per action).
- `work/` — cwd of each `claude -p` run (gitignored).

Code:

- `stages/<stage>.md` — the procedure Claude follows for a project; the launcher passes its path in the prompt. Each must define a resume rule.
- `scripts/router.py` — one tick: hours check (01:00–06:59; `--now` skips it), tmux lock (`agent-pm`), prune, Recover across runnable projects, plan, usage probe only when there is work, then resume an interrupted In Progress issue or claim a new one (priority, then later stage, then oldest) and call `launch.py`. Both need the probe not rejected, 5-hour usage < 90% and weekly windows not full (`MAX_5H`). `--dry-run` changes nothing and starts no run; `--now --issue ID` starts a specific Todo issue; `--pick [--project NAME]` = Recover + Pick + Claim (stage step 1). Unknown flags exit 2.
  - Scheduled hourly 01:00–06:00 by `~/Library/LaunchAgents/com.ophis.agent-pm.router.plist`; source: `scripts/com.ophis.agent-pm.router.plist`.
  - Watch: `tmux attach -t agent-pm`. Inspect a run: `cd work && claude --resume <session-id>`.
- `scripts/launch.py` — starts one run from the project's `pipeline.toml` entry (instructions, model, effort, add_dirs) in tmux; called by the router.
- `scripts/pipeline.py` — shared: Linear API, paths, config. Needs the agent account's API key in the macOS Keychain:
  - Store: `security add-generic-password -a frank.agent.w -s linear-api-key -w` (prompts for the key).
  - Read: `security find-generic-password -a frank.agent.w -s linear-api-key -w` per call; the key is sent only as the `Authorization` header to `api.linear.app`, never printed or written to disk. Works under launchd because the LaunchAgents run in the login session.
- `scripts/promote.py` — Handoff: for each issue in the `Handoff` status whose project has a `next` in `pipeline.toml`, creates the next stage's issue (Todo, related, with the source links, the `human_members`' comments since the last review as instructions, and every source comment quoted for context) and moves the source to Done; no comment → back to In Review with a question, unless the source project sets `require_instructions = false`. Enabled: Deep Research → Product Design (PRD), Product Design → Engineering (TDD, comments optional). No LLM. `--dry-run` changes nothing. Runs at :05, :20, :35 and :50 via `com.ophis.agent-pm.promote` (source `scripts/com.ophis.agent-pm.promote.plist`, `/opt/homebrew/bin/python3`). Design: `docs/specs/2026-09-27-promote-design.md`.
- `pipeline.toml` — team, `human_members`, and per project `next` (the stage a Handoff creates), `prefix` (title prefix of issues created there), `require_instructions` (default true), and for runnable projects `instructions`, `model`, `effort`, `add_dirs`.
- `scripts/tests/` — `python3 -m unittest discover -s scripts/tests` (Python 3.11+; no network, Keychain or Claude).
- Resuming (Deep Research): the router resumes the same session (`claude -p --resume <sid>`) with a prompt to re-read `stages/deep-research.md` and follow its resume rule. The resumed session reuses the interrupted deep-research run's saved result (or its journal if the run was killed) and re-runs only missing agents (e.g. 3 fresh votes for a claim with fewer than 2 valid votes); it never uses `resumeFromRunId` (that replays only the unchanged prefix of agent calls, so deep-research re-runs almost everything) and never moves the issue to Todo.
- Design: `docs/specs/2026-09-27-auto-resume-design.md`, amended by `docs/specs/2026-09-27-resume-candidate-fix-design.md`, `docs/specs/2026-09-27-resume-from-saved-result-design.md` and `docs/specs/2026-09-27-router-launcher-design.md`.

Notes:
- `launch.py` sets `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`; `claude -p` otherwise kills the research workflow after 10 idle minutes.
- One research run costs roughly 40% of the 5-hour window at `--effort xhigh`.

## Rescheduling

Edit `StartCalendarInterval` in `scripts/com.ophis.agent-pm.router.plist`, copy it to `~/Library/LaunchAgents/`, then reload (launchd reads the plist only at load). Keep the hours window in `router.py` in sync. The same steps install or reload `com.ophis.agent-pm.promote`. launchd does not create `logs/` and cannot start a job whose log directory is missing, so create it first.

```bash
mkdir -p ~/playground/agent-pm/logs
P=~/Library/LaunchAgents/com.ophis.agent-pm.router.plist
plutil -lint $P
launchctl bootout gui/$(id -u)/com.ophis.agent-pm.router
launchctl bootstrap gui/$(id -u) $P
launchctl print gui/$(id -u)/com.ophis.agent-pm.router | grep -A3 descriptor   # confirm the new time
```

Stop the schedule: `launchctl bootout gui/$(id -u)/com.ophis.agent-pm.router` (plus delete the plist, or it loads again at next login).
