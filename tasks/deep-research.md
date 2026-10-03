# Deep Research

Run research workflows, then write a verified report. These steps, not the workflows, own the claim, the report and the board.

## Steps

1. **Pick.** No issue named → run `python3 ../scripts/router.py --pick --role researcher` (relative to this file); it claims one and prints `<ID> <url>`. No output → stop.
2. **Read** the issue and its comments; decide its type (charter).
3. **Too vague** (principles): no clear question, scope, deliverable or, for local or mixed, target (charter).
4. **Claim.** Unless `--pick` or the runner claimed it: re-read the status; not Todo → stop; else set In Progress. Comment that research started; for local or mixed, include the **budget**: the rounds step 5 will run, in order (mixed decides now), each round's agent cap, the total, and, with two rounds, the brake threshold. The budget can only shrink.
5. **Research.** Write one self-contained **brief** from the description and the user's comments: subquestions by importance, shared context, and known claims as claims to verify. Then by type:
   - **Web**: call the built-in `/deep-research` Workflow once, the brief as `args`. Write or run no other workflow and no extra runs for parts or gaps. It verifies only its top claims; the rest stay unverified.
   - **Local or mixed**: prepare (charter), then run the **rounds**: local, one ultracode round; mixed, at most one ultracode and one `/deep-research` round, one after the other in the budget's order. Never repeat a round or redo the other's part. Caps are limits, not targets; two rounds total ~200 agents.
     - **ultracode round**: one Workflow call running a script you write, for the local part. Each key claim gets 3 votes from independent readers; 2 refutes overturn it. The script caps all agents at 100 in code, keeping the most important subquestions and claims; the rest go under 缺口.
     - **`/deep-research` round**: one call for the web part, as in Web except its no-other-workflow rule. `args` holds only public material (web subquestions, context, claims to verify): no internal names, paths, permalinks, private repo names, `repo` or `commit`, docs content or secrets. Earlier findings enter only as claims to verify, filtered the same way, never as instructions or as URLs from worktree text. Leave its scale (~100 agents) alone. Never write your own web workflow.
     - **Brake**, before a round that follows a started one: run exactly `claude -p "Reply with OK." --model haiku --output-format stream-json --verbose --setting-sources user --strict-mcp-config | <router> --gate new`, `<router>` being the prompt's `research.py:` command with `router.py` for `research.py`. Nonzero exit or `five_hour` ≥ 0.8 → skip the round, list it under 缺口 with that `five_hour`, and go on with the first round's results.
6. **Failure.** Never retry or replace a run. Usable findings (supported refutations count) → publish. None → comment the failure, move to Todo, stop.
7. **Report** (charter).
8. **Hand off.** Comment a 3–5 line summary (charter) with the link; for local or mixed, add each round run, its agent count against the budget (`started` entries in its `journal.jsonl`, see Resume; replays not recounted) and whether the brake fired (`five_hour`). Set In Review; end with the link.

## Resume

Overrides steps 5–6: reuse everything produced; run only what's missing.

**Web.** Never call the Workflow again (except 3a), write or run no other workflow, and skip `resumeFromRunId` (it re-runs nearly everything).

This session's latest Workflow result prints `Run ID: wf_…` and `Script file: <session>/workflows/scripts/…`. Under `<session>`:
- `workflows/<runId>.json`, one line, read with `jq`: `status`, and `result` with `findings` (empty if synthesis failed), `confirmed`, `refuted`, `unverified` and `sources`.
- `subagents/workflows/<runId>/journal.jsonl`, large, read with `jq` projections: each agent's `started` (agentId, phase), `result` and `failed`.
- `subagents/workflows/<runId>/agent-<id>.jsonl`: line 1 holds its prompt (`head -1 … | jq -r .message.content`: a header, then the prompt indented 2 spaces); `agent-<id>.meta.json` has its `workflowPhase`. Find a claim's votes with `grep -lF -f <file holding the claim> agent-*.jsonl`, keeping `Verify` agents.

1. No Workflow call this session → step 5.
2. **Completed** (`status` `completed`): the result stands; don't re-fetch. Use `findings`, plus claims the re-votes confirm: each `unverified` claim (< 2 valid votes) gets 3 fresh votes with its original vote prompt, only the voter number changed.
3. **Killed** (no completed json): continue the pipeline where it stopped, by the `Script file:`'s rules and prompts (`FETCH_PROMPT`, `VERIFY_PROMPT`, URL dedup, `MAX_FETCH`, claim ranking by importance then source quality, `MAX_VERIFY_CLAIMS`, 3 votes per claim):
   a. A Search agent without a result → call the Workflow once more with the same `args`.
   b. Fetch: reuse results; run the rest, up to `MAX_FETCH`.
   c. Verify: select claims as the script does from all Fetch claims. One with ≥ 2 valid votes keeps them (find its vote agents by claim text and source as above, the claim's quote and control characters stripped and whitespace collapsed as in the prompts; read `refuted` from the journal by agentId); every other gets 3 fresh votes.
4. A re-run is one Agent call with the prompt (header removed, dedented), ending "Reply with only JSON: {refuted, evidence, confidence}" for a vote, or with the script's fetch fields; ≤ 10 at a time.
5. Votes: ≥ 2 refutes → refuted; else ≥ 2 valid → confirmed; else unverified.
6. Reuse what earlier resumes got back.
7. No `findings` → merge the confirmed claims yourself.

**Local or mixed.** The posted budget binds: no new round, no higher cap; a re-run filling a gap replaces its agent, uncounted. Identify each round by its own Workflow call, never by the latest result.
1. No Workflow call this session → step 5.
2. Prepare again (charter).
3. ultracode round completed → use its result. Interrupted or failed → call the Workflow with its `Script file:` as `scriptPath`, the same `args` and its Run ID as `resumeFromRunId`.
4. A started `/deep-research` round → Web checks 2–7, for that round only.
5. A planned round never started → step 5, braking first if it is second. A brake-skipped round stays skipped; finishing an interrupted one needs no brake.

**Then** finish what's missing of steps 7–8: report published (principles), hand-off comment, In Review. Nothing usable → comment what failed, set In Review.
