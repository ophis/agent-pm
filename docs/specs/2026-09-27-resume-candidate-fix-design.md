# Resume candidate fix

Status: design, reviewed (spec fitness, architecture, scheduler edge cases: all PASS). Amends `2026-09-27-auto-resume-design.md`.

## Problem

`pick.py` `candidate()` only considers the SID of the globally latest `start`/`resume` line in runs.log. If issue A is interrupted and is still live (session files written < 30 min ago) at the next tick, the plan claims a new issue B. From then on the latest line belongs to B, so A is never a resume candidate again. Recover later returns A to Todo (`updatedAt` > 2h) and it restarts from scratch, losing progress and using an attempt.

The same loss happens with two resumable issues: Recover exempts only the chosen candidate, so the other one is returned to Todo.

## Change

1. **One resumability check.** `resumable(issue)` takes an agent-owned In Progress issue (precondition, not re-checked) and returns `(sid, k)` or `None`. It returns `(sid, k)` when all hold:
   - `sid = latest_sid(entries, issue)` exists;
   - `<tdir>/<sid>.jsonl` exists;
   - `k = resume_count(entries, sid) + 1 <= MAX_RESUMES`;
   - `attempts(issue) < CAP`;
   - the session is not live (30-min mtime rule, unchanged).
2. **Recover computes it once per issue** and reuses the result (no second `latest_sid` / `resume_count` / `attempts` call, which can hit the Linear history API). Per issue:
   - live → skip;
   - resumable → skip (collected as a candidate);
   - attempt cap reached → comment and In Review;
   - `updatedAt` > 2h → comment and Todo (as today). Issues with no SID in runs.log keep the plain 2h rule.
3. **Candidate** = the collected resumable issue with the lowest sort key: `(priority or 5, first_line_time(sid))`. The priority part is a helper shared with `claim()` (which then sorts Todo by `createdAt`). `first_line_time(sid)` is the timestamp of that SID's `start` line, or of its first line of any kind if the `start` line is missing (partial runs.log loss).
4. **Plan output** is unchanged: `resume <ISSUE> <SID> <k> <url>`, else `new` if Todo is non-empty, else nothing.

## Resulting behavior

- An interrupted issue is eventually resumed, in priority / oldest order, even if other issues were started in between. One run at a time: other resumable issues wait while a run is in tmux, and hours or the usage gate can push a resume to the next night.
- An issue whose session never wrote a transcript (launch failed) is not resumable; Recover returns it to Todo after 2h.
- An issue that used its 2 resumes is not resumable; Recover returns it to Todo after 2h (restart). The attempt cap still sends it to In Review at 4 attempts.

## Not changing (decided)

- No "wait while another issue is live" rule: new work may be claimed while an interrupted issue is still in its 30-min window; the fix guarantees that issue is picked up later.
- No `end`-line liveness; the 30-min mtime rule stays.
- Accepted risks:
  - A hung `claude` inside tmux blocks every tick (tmux check).
  - A manual `claude --resume <SID>` left idle > 30 min (e.g. at a permission prompt) counts as not live, so the runner may resume the same SID in parallel. This now applies to any agent-owned In Progress issue, not only the latest one. The same holds if the user moves a finished issue back to In Progress while it is still assigned to the agent.
  - A resumable high-priority issue that keeps failing the usage gate delays lower-priority resumes (one candidate per tick).

## Tests (`tests/test_pick.py`)

Setup rule: every issue meant to be resumable gets `<sid>.jsonl` older than 30 min.

- Replace `test_only_latest_line_is_candidate` (it encodes the bug).
- Fix fixtures that relied on the old rule: `test_attempt_cap_reset_by_user` and `test_resume_cap_falls_back_to_recover` create their `<sid>.jsonl` (older than 30 min), so each still tests what its name says.
- Original bug, two ticks: tick 1, A live and Todo non-empty → `new`, A untouched; tick 2 (A not live, B In Progress) → `resume A`, no mutations.
- Ordering, naming the expected issue: priority beats age; equal priority → oldest first line; priority 0 ranks lowest.
- A and C both resumable, both `updatedAt` > 2h → one resumed, neither moved by Recover.
- A resumable next to a stale, resume-capped C → C to Todo, `resume A`.
- An attempt-capped issue whose line is not the latest in runs.log → In Review.
- A with a missing `<sid>.jsonl` → not a candidate; Recover returns it to Todo after 2h.
- SID with `resume` lines but no `start` line → sorted by its first line.
