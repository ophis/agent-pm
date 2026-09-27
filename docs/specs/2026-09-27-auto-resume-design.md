# Auto-resume for the Deep Research runner

Status: design, approved in conversation 2026-09-27; not implemented.

## Goal

A research run that stops before handing off (usage limit, API error, crash, reboot) continues the same Claude session on a later tick instead of restarting from scratch. The runner stays stateless: every tick decides from `runs.log`, Linear and the usage probe.

## Schedule

- launchd fires `linear-research.sh` hourly at 23:00–06:00 (replaces 23:59 / 05:10).
- The script only starts work between 23:00 and 06:59 and logs a skip otherwise (launchd runs a missed fire once on wake). `--now` skips the hours check for manual runs; every other check still applies.

## Tick

1. Outside the hours → skip.
2. tmux session `linear-research` exists → skip.
3. `pick.py --plan RUNS_LOG`: always runs Recover, then prints `resume <ISSUE> <SID> <k>`, `new`, or nothing. No claim.
4. Nothing → skip (no probe).
5. Probe usage (existing Haiku call). No `rate_limit_event` → skip.
   - `resume` needs status ≠ `rejected`, `five_hour` < 0.8, and every `seven_day*` window < 1.
   - `new` needs `five_hour` < 0.30.
6. `resume` → append `resume <ISSUE> session=<SID> n=<k>` to runs.log, then start tmux with the resume command.
   `new` → `pick.py --claim` (Pick + Claim), then start as today.
7. The tmux command appends `end <ISSUE> session=<SID> exit=<code>` after `claude` exits (debugging only).

`--dry-run` runs `pick.py --plan` without changes and prints the probe result.

## State

- `runs.log` is the only source of (ISSUE, SID) and of the counts. Parsed lines: `<ts> start <ISSUE> session=<SID> …` and `<ts> resume <ISSUE> session=<SID> n=<k>`; all others are ignored.
- runs.log lost → nothing is resumable and no attempt cap applies; Recover and restart handle everything.

## Resumable

The SID of the latest `start`/`resume` line is resumable when all hold:
- its issue is In Progress and assigned to the agent;
- the SID has fewer than 2 `resume` lines and the issue is below the attempt cap;
- no file under `<sid>.jsonl` or `<sid>/` in the runs' transcript folder changed in the last 30 min (a manual or still-running session is live).

## Recover (pick.py)

For each agent-owned In Progress issue:
- skip if its latest SID's files changed < 30 min ago, or it is the resume candidate;
- if it has reached the attempt cap and is not the resume candidate → comment "Tried 4 times without finishing; needs a look." and set In Review;
- otherwise, if `updatedAt` > 2h → comment and return to Todo (as today).
Issues with no SID in runs.log keep the plain 2h rule.

## Attempt cap

- Attempts counted = `start <ISSUE>` and `resume <ISSUE>` lines after the latest time a non-agent user moved the issue to Todo (Linear issue history). Moves by the agent (Recover, a failed run) do not reset it.
- Pick moves a Todo issue with 4 or more counted attempts to In Review with that comment and takes the next one (covers a run that sent the issue back to Todo itself).
- An In Progress issue that reaches 4 counted attempts goes to In Review with that comment (see Recover).
- Anything that needs the user goes to In Review, the only column the user watches. Issues are never moved to Backlog.

## Resume command

Same cwd (`work/`), env (`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`), model, effort, permission mode and `--add-dir` as a start, with `--resume <SID>` instead of `--session-id`. Prompt: `Resumed run <k>/2 for <ISSUE> (<url>) after an interruption. Follow the linear-deep-research skill's resume rule.`

## SKILL.md changes

- Step 3 (too vague): comment the questions and set **In Review** (not Backlog). Board statuses: Backlog is no longer used by the agent; In Review means "needs the user" (report ready, questions, or stuck).
- New resume rule, the one exception to steps 5–6, checked in order:
  1. No Workflow call yet in this session → continue from step 5; it is still the single run.
  2. The run's `…/<sid>/subagents/workflows/<runId>/journal.jsonl` shows agents that failed on rate_limit, or has no final result → `resumeFromRunId` once. Judge from the journal only, never `/private/tmp`.
  3. Otherwise → Report, doing only what is missing: report committed and pushed, link on the issue, hand-off comment, In Review.
  Never repeat the "research started" comment. Failures left after the resume → publish with Gaps.

## Code layout

- `pick.py`: `--plan` (Recover + decision), `--claim` (Pick + Claim + attempt cap), `--dry-run`. Parsing, liveness and cap logic are plain functions.
- `linear-research.sh`: thin dispatcher (hours, tmux, plan, probe, launch).

## Tests

- Unit tests for pick.py with a fake `gql`, temp runs.log and a temp transcript folder: every plan branch, liveness, resume cap, attempt cap and its reset, `--now`, Recover exclusions.
- Dispatcher tests with a fake `claude` on PATH and a stubbed probe: each tick branch.
- Near-free real checks: `--session-id X` then `-p --resume X` with Haiku grows one .jsonl; the probe event has `status`, `utilization`, `unifiedWindows`.
- Write-proof rehearsal of rule 3: `claude -p <resume prompt> --resume 3f597305-673d-48a5-a7b6-4bf6d73ac2cb --fork-session --model haiku --permission-mode dontAsk --allowedTools "Read,Grep,Glob,mcp__linear-server__get_issue,mcp__linear-server__list_comments"`; expect it to report everything done.
- Rules 1–2 are verified on the first real resume by reviewing its transcript.

## Implementation notes (as built, PR ophis/claude#1)

- `pick.py --plan` prints `resume <ISSUE> <SID> <k> <url>`; the URL feeds the resume prompt.
- The probe gates live in `pick.py --gate resume|new` (reads the probe's stream-json on stdin).
- A `seven_day*` window without a utilization value passes the resume gate.
- Known gaps (review, not fixed): a session without `<sid>.jsonl` is still resumable; the `new` gate ignores `rejected` and full weekly windows; `--dry-run` plan does not apply Recover.
