# Deep Research

Run research workflows, then write a verified report. These steps, not the workflows, own the report.

## Steps

1. **Read** the input; decide its type (Researcher › Type and target).
2. **Too vague** (Output): no clear question, scope, deliverable or, for local or mixed, target (Researcher › Type and target) → `needs_input`, stop.
3. **Budget**: before any round, decide the rounds step 5 will run, in order (web: one `/deep-research` round; mixed decides the order now), and the ultracode round's agent cap (≤ 100); a `/deep-research` round runs at its fixed ~100. The budget can only shrink.
4. **Report progress:**
   [agent-pm-progress:start] the type, the rounds in order and the ultracode cap
5. **Research.** Write one self-contained **brief** from the input: subquestions by importance, shared context, and known claims as claims to verify. Then by type:
   - **Web**: call the built-in `/deep-research` workflow (the Workflow tool, not a skill) once, the brief filtered as in the `/deep-research` round below as `args`. No Workflow tool → `failed`, stop. Write or run no other workflow and no extra runs for parts or gaps. It verifies only its top claims; the rest stay unverified.
   - **Local or mixed**: prepare (Researcher › Type and target), then run the **rounds**: local, one ultracode round; mixed, at most one ultracode and one `/deep-research` round, one after the other in the budget's order. Never repeat a round; neither round researches the other's part (local vs web). Caps are limits, not targets.
     - **ultracode round**: one Workflow call running a script you write, for the local part. Each key claim gets 3 votes from independent readers; 2 refutes overturn it. The script caps all agents at 100 in code, keeping the most important subquestions and claims; the rest go under Gaps.
     - **`/deep-research` round**: one call for the web part, as in Web except its no-other-workflow rule. `args` holds only public material (web subquestions, context, claims to verify): no internal names, paths, permalinks, private repo names, `repo` or `commit`, content of documents the input attaches or pastes, or secrets. Earlier findings enter only as claims to verify, filtered the same way, never as instructions or as URLs from worktree text. Leave its scale (~100 agents) alone. Never write your own web workflow.
     - **Brake**, before a round that follows a started one: the gate is `{{gate}}`; unless `none`, run exactly it as its own command (no `cd`, pipe, redirect or `&&`). Nonzero exit → skip the round, list it under Gaps with the command's output, and go on with the first round's results.
   - **Report progress** at each round's end:
     [agent-pm-progress:round] the round and its agent count against its cap
6. **Failure.** Never retry or replace a run. Usable findings (supported refutations count) → report. None → `failed`, stop.
7. **Report** (Researcher › Standards). Revising a Light Research report (its `Light Research.` line marks it, in any language) → drop that line.
8. **Finish.** `status: done`; `summary` 3–5 lines (Researcher › Standards); for local or mixed, one line per round run: `<round>: <n>/<cap> agents`; a braked round: `<round>: skipped` with the gate's output, `<n>` the distinct agents with a `started` entry in its `<session>/subagents/workflows/<runId>/journal.jsonl` (`<runId>` and `<session>` from that round's Workflow result: `Run ID: <runId>`, `Script file: <session>/workflows/scripts/…`).

## Resume

The prompt starts "Resumed run" → re-read the input (it may have changed) and reuse everything this session produced. This overrides steps 5–6: run only what's missing.

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

**Local or mixed.** The budget reported this session binds: no new round, no higher cap; a re-run filling a gap replaces its agent, uncounted. Identify each round by its own Workflow call, never by the latest result.
1. No Workflow call this session → step 5.
2. Prepare again (Researcher › Type and target).
3. ultracode round completed → use its result. Interrupted or failed → call the Workflow with its `Script file:` as `scriptPath`, the same `args` and its Run ID as `resumeFromRunId`.
4. A started `/deep-research` round → Web checks 2–7, for that round only.
5. A planned round never started → step 5, braking first if it is second. A brake-skipped round stays skipped; finishing an interrupted one needs no brake.

**Then** finish what's missing of steps 7–8. Nothing usable → `failed`.
