# Resume from the saved workflow result

Status: design v2, reviewed (fitness, run-file facts, skill usability: all PASS). Replaces the SKILL resume rule 2 of `2026-09-27-auto-resume-design.md` (`resumeFromRunId`). A first version of the SKILL text is on main (~/.claude c9fa26e); this version supersedes it.

## Problem

A resumed session called `Workflow({resumeFromRunId})`. The Workflow tool replays only "the longest unchanged prefix of agent() calls"; deep-research starts Fetch agents in completion order, so the prefix ends after Search and a resume re-runs almost the whole workflow (TASK-15: 23 Fetch and 26+ Verify re-run for 4 failed votes and a failed synthesis).

The resume was also unnecessary. deep-research always returns a result: when synthesis fails it returns `confirmed`, `refuted`, `unverified` and `sources`, with a summary "Synthesis step was skipped or failed". Claude Code saves the finished run as `workflows/<runId>.json` (status `completed`). Checked on all 7 local runs whose synthesis failed. The resumed session looked only at `journal.jsonl`, whose `failed` entries also appear in runs that completed.

## Rule

A resumed session ("Resumed run …") first re-reads the current `SKILL.md` (its context may hold an older version of this rule; the current file wins). It never calls the Workflow again and never uses `resumeFromRunId`. Checked in order:

1. No Workflow call yet in this session → continue from step 5; it is still the single run.
2. Find the run: the `Run ID: wf_…` in the result of this session's latest Workflow call for this issue. The same result prints `Script file: …/workflows/scripts/…`; the run file is `<that workflows dir>/<runId>.json` (this also works in a forked session). Read it with `jq` (one long line; do not Read or grep it): `jq '{status, summary: .result.summary, findings: (.result.findings|length), confirmed: (.result.confirmed|length), refuted: (.result.refuted|length)}'`, then extract the fields needed.
3. `status` is `completed` and the result has at least one finding, confirmed claim or refuted claim (a supported refutation is a usable finding, as in step 6) → write the report from `result`: `summary`/`findings` when synthesis succeeded, else merge `confirmed` (claim, source, quote, vote) yourself; `refuted` claims are reported as refutations (under Corrections when the issue listed them as already known, else under Gaps); `unverified` go under Gaps. No votes are re-run.
4. Otherwise (no file, `status` other than `completed`, or zero findings, confirmed and refuted claims) → step 6's no-usable-findings path: comment the failure, move the issue to Todo, stop. A later tick starts a fresh single run, unless the issue has reached the attempt cap (then Pick moves it to In Review).
5. After rule 3: steps 7–8, doing only what is missing: report committed and pushed, link on the issue, hand-off comment, In Review. Never repeat the "research started" comment.

## Runner change

`linear-research.sh` resume prompt: `Resumed run <k> for <ISSUE> (<url>) after an interruption. Re-read ${CLAUDE_SKILL_DIR}/SKILL.md — it may have changed since this session started — and follow its resume rule.` The skill dir is resolved by the script (the directory containing `scripts/`). Dispatcher test updated.

## Not doing

- Re-running failed votes (claim ↔ vote matching is unreliable: labels are truncated and collide; legacy journals mix two runs).
- Salvaging a killed run from `journal.jsonl`.
- Both can be added later if runs.log shows them worth it.

## Verification

- Dispatcher unit test: resume prompt text.
- Rehearsal: from `~/playground/linear-research/work`, fork TASK-15's session: `claude -p "<new resume prompt, k=2, TASK-15> Read-only rehearsal: say which rule applies and what you would do; do not write anything." --resume 1b48b9a1-d118-4319-a285-904b45200fb5 --fork-session --model haiku --permission-mode dontAsk --add-dir ~/.claude --allowedTools "Read,Grep,Glob,Bash(jq:*),mcp__linear-server__get_issue,mcp__linear-server__list_comments"`. Pass: it re-reads SKILL.md, picks rule 3 for `wf_0059deae-35d` (completed, 23 confirmed), and the forked transcript has no `Workflow` or `Agent` tool call.
- If the fork to Haiku is rejected (signed Opus thinking blocks in the transcript), rerun the rehearsal with `--model opus`; that is not a rule failure.
- The first real resume after merge is reviewed against the rule.
