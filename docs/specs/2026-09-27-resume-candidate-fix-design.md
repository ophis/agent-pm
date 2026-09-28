# Resume candidate fix

Status: implemented (PR ophis/claude#3); design v4 reviewed (spec fitness, architecture, scheduler edge cases: all PASS). Amends `2026-09-27-auto-resume-design.md`.

## Problem

`pick.py` `candidate()` only considers the SID of the globally latest `start`/`resume` line in runs.log. It assumes at most one interrupted run exists. The 30-min liveness guard breaks that assumption: while interrupted issue A is still live, the plan claims a new issue B, the latest line becomes B's, and A is never resumed. Recover later returns A to Todo and it restarts from scratch.

## Rule

"Resume first, then start new" stays; "which issue to resume" changes from "the latest line" to "every agent-owned In Progress issue".

### Current SID

`current_sid(issue)` = the SID of the issue's latest `start`/`resume` line, but only if that SID's first line is at or after the issue's latest move to In Progress (Linear issue history) minus 5 min. Otherwise the issue has no current SID. If the history shows no move to In Progress, the SID is current. One history fetch per issue per tick serves both this bound and the attempt-cap reset time. The fetch requests history newest first, so the latest move to In Progress is always on the first page. An SID from an earlier claim (the issue went back to Todo and was claimed again, e.g. by hand through the skill, which writes no runs.log line) is never used.

### Walk

`Board.recover()` walks every agent-owned In Progress issue in order `(no current SID, priority or 5, first_line_time(sid))`: issues without a current SID last, then priority (0 = none = lowest), then oldest first line. For each issue the first matching rule wins:

1. **Live** (a file under `<sid>.jsonl` or `<sid>/` changed in the last 30 min) → leave it.
2. **Attempt cap**, only for an issue with a current SID (≥ 4 `start` + `resume` lines since the user last moved it to Todo) → cap comment, In Review. An issue without a current SID (e.g. claimed by hand) is never capped here; if it returns to Todo, Pick's cap applies.
3. **Current SID and `<tdir>/<sid>.jsonl` exist** → the first such issue is the candidate; later ones are left alone this tick.
4. **Current SID, no `<sid>.jsonl`, SID's latest line older than 30 min** (the launch failed) → interrupted comment, Todo, unassign.
5. **No current SID** → if `updatedAt` > 2h: interrupted comment, Todo, unassign; else leave it.

The walk continues past the candidate so rules 1, 2, 4 and 5 apply to every issue each tick. `recover()` returns the candidate.

- `--plan`: `resume <ISSUE> <SID> <k> <url>` for the candidate, where `k` = that SID's `resume` lines + 1; else `new` if Todo is non-empty; else nothing.
- No mode (skill step 1: Recover + Pick + Claim): the same walk with the candidate left alone, then Pick + Claim as today.
- `--dry-run`: the walk reports what it would do and changes nothing.

Definitions: `first_line_time(sid)` = timestamp of the SID's `start` line, or of its first line of any kind if `start` is missing. The priority part of the sort is a helper shared with `claim()`, which keeps sorting Todo by `createdAt` after priority. The comments and the unassign reuse today's Recover text and `assigneeId=None`.

## runs.log retention

- Owner: a new `pick.py --prune RUNS_LOG` mode, called by `linear-research.sh` as its own step after the tmux check and before `--plan`, with its stderr not redirected into runs.log. A tick that skips at the hours or tmux check does not prune; `--dry-run` does not prune.
- It drops lines older than 7 days: writes a temp file in the runs.log directory, then renames it over runs.log (atomic on the same filesystem). No other pick.py mode rewrites runs.log; they only append. A missing runs.log is a no-op; a prune failure is non-fatal: the dispatcher appends `<ts> skip: prune failed` and the tick continues.
- A line without a timestamp takes the timestamp of the nearest earlier timestamped line; untimestamped lines before the first timestamped line count as older than 7 days. `pick.py` log lines get a timestamp from now on.
- Effects: the attempt cap counts within 7 days; an issue whose lines were all pruned has no current SID (rule 5).

## Removed

- The per-session resume cap (`MAX_RESUMES = 2`). The attempt cap bounds retries; a session is resumed until it finishes or the issue reaches 4 attempts.
- Recover's 2h rule for issues with a current SID. They are resumed (rule 3) or returned (rule 4). The 2h rule remains for rule 5.
- The "restart from scratch after 2 failed resumes" path.

## Other changes

- Resume prompt: `Resumed run <k> for <ISSUE> (<url>) after an interruption. Follow the linear-deep-research skill's resume rule.` (no `/2`; `linear-research.sh`).
- The parent spec's Tick (prune step), State (7-day window), Resumable, Recover, Attempt cap, Resume command, Code layout (`--prune`) and Tests sections are rewritten to match this amendment.
- Log line formats, gate, hours, tmux check, claim and the SKILL resume rule are otherwise unchanged.

## Resulting behavior

- An interrupted issue is eventually resumed, in priority / oldest order, even if other issues were started in between. One run at a time; hours or the usage gate can push a resume to the next night.
- New work is claimed only when no agent-owned In Progress issue is resumable. A live issue does not block new work.
- A poison issue stops at 4 attempts (within 7 days) and goes to In Review.

## Accepted risks

- A hung `claude` inside tmux blocks every tick.
- A manual `claude --resume <SID>` idle > 30 min counts as not live; the runner may resume the same SID in parallel. The same holds if the user moves a finished issue back to In Progress while it is still assigned to the agent and within 5 min of its current SID.
- One candidate per tick: a high-priority resumable issue that keeps failing the gate, or a resume that fails fast (e.g. a corrupt transcript), delays lower-priority work for at most 4 attempts, then goes to In Review.
- An issue interrupted and not resumed within 7 days loses its SID; rule 5 returns it to Todo and it restarts from scratch (it is not orphaned).
- The attempt cap counts within the 7-day window, so it limits the rate rather than the total: a poison issue whose attempts are spread out (≤ 3 per 7 days, e.g. by the usage gate) keeps retrying.
- Two concurrent ticks (a manual `--now` tick next to a scheduled one) can lose a line appended during pruning.

## Tests (`tests/test_pick.py`, plus one dispatcher test)

Setup rule: a resumable issue gets `<sid>.jsonl` older than 30 min and a Linear history whose latest move to In Progress precedes its SID's first line.

- Replace `test_only_latest_line_is_candidate`; remove or rewrite tests asserting the resume cap of 2 or the 2h rule for issues with a current SID; fix fixtures relying on the old candidate rule (e.g. `test_attempt_cap_reset_by_user` creates `d.jsonl`).
- Original bug, two ticks: tick 1, A live and Todo non-empty → `new`, A untouched; tick 2, A not live, B In Progress → `resume A`, no mutations.
- Ordering, naming the expected issue: priority beats age; equal priority → oldest first line; priority 0 ranks lowest; an issue without a current SID never outranks one with a current SID.
- One tick with changes and a candidate: A without jsonl (old) → Todo, D capped → In Review, C → `resume C`; an issue after the candidate that is capped is also moved.
- A live, C resumable → `resume C`; A untouched.
- A reaches 4 attempts → In Review, not resumed.
- SID without jsonl: latest line 10 min old → untouched; 40 min → Todo.
- No current SID: `updatedAt` 1h → untouched; 3h → Todo.
- Stale SID: old SID with jsonl, issue moved to In Progress by hand 3h after that SID's last line → not resumed; handled by rule 5.
- Tolerance: start line a few seconds before the Linear move → current; 6 min before → not current. No move to In Progress in the history → current.
- An issue without a current SID and ≥ 4 old attempts, In Progress by hand → not moved to In Review.
- Launch failure over several ticks: rule 4 → Todo → claimed again; on the 4th start Recover's rule 2 sends it to In Review (as built; Pick's cap is not reached first).
- User reset: capped issue moved to Todo, claimed again → the new SID is resumed; the old SID is ignored in ordering and in `k`.
- `k` counts only that SID's resume lines when an older SID of the same issue has resume lines.
- SID with `resume` lines but no `start` line → sorted by its first line.
- No-mode Recover with several In Progress issues: same moves as `--plan`, the candidate untouched, then Pick + Claim.
- Dry-run of a walk with changes before the candidate → no mutations, same plan output.
- Retention (`--prune`): lines older than 7 days dropped; untimestamped lines follow their predecessor; leading untimestamped lines dropped; temp file in the same directory, renamed over runs.log; attempt count after pruning.
- Prune: missing runs.log → no-op; a failing `--prune` → `prune failed` line and the tick continues.
- Dispatcher: resume prompt has no `/2`; `--prune` runs after the tmux check and before `--plan`, not on hours/tmux skips, not under `--dry-run`.
