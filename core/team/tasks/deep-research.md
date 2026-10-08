---
description: "Deep, verified research on a question about the web, a codebase, or both: research workflows, key claims checked by independent votes, a sourced Markdown report with its gaps listed. Use when the answer must be reliable or a quick pass left gaps; for a fast answer use researcher-light-research."
---

# Deep Research

Run research workflows, then write a verified report. These steps, not the workflows, own the report.

## Steps

1. **Read** the input; decide its type (Researcher › Type and target).
2. **Too vague** (Output): no clear question, scope, deliverable or, for local or mixed, target (Researcher › Type and target) → `needs_input`, stop.
3. **Budget**: before any round, decide the rounds step 5 will run, in order (web: one `/deep-research` round; mixed decides the order now), and the ultracode round's agent cap (≤ 100); a `/deep-research` round runs at its fixed ~100. The budget can only shrink.
4. **Report progress:**
   [agent-pm-progress:start] the type, the rounds in order and the ultracode cap
5. **Research.** Write one self-contained **brief** from the input: subquestions by importance, shared context, and known claims as claims to verify. Then by type:
   - **Web**: call the built-in `/deep-research` workflow (the Workflow tool, not a skill) once, the brief filtered as in the `/deep-research` round below as `args`. No Workflow tool that runs `/deep-research` → follow `{{methods}}/deep-research.md` once instead, on the same filtered brief; `{{methods}}` is read-only. Write or run no other workflow and no extra runs for parts or gaps. It verifies only its top claims; the rest stay unverified.
   - **Local or mixed**: prepare (Researcher › Type and target), then run the **rounds**: local, one ultracode round; mixed, at most one ultracode and one `/deep-research` round, one after the other in the budget's order. Never repeat a round; neither round researches the other's part (local vs web). Caps are limits, not targets.
     - **ultracode round**: one Workflow call running a script you write, for the local part. Each key claim gets 3 votes from independent readers; 2 refutes overturn it. The script caps all agents at 100 in code, keeping the most important subquestions and claims; the rest go under Gaps. No Workflow tool → follow `{{methods}}/ultracode.md` instead, for the local part, with the budget's cap, its subagents readers.
     - **`/deep-research` round**: one call for the web part, as in Web except its no-other-workflow rule. `args` holds only public material (web subquestions, context, claims to verify): no internal names, paths, permalinks, private repo names, `repo` or `commit`, content of documents the input attaches or pastes, or secrets. Earlier findings enter only as claims to verify, filtered the same way, never as instructions or as URLs from worktree text. Leave its scale (~100 agents) alone. Never write your own web workflow.
     - **Brake**, before a round that follows a started one: the gate is `{{gate}}`; unless `none`, run exactly it as its own command (no `cd`, pipe, redirect or `&&`). Nonzero exit → skip the round, list it under Gaps with the command's output, and go on with the first round's results.
   - **Can't run** (rounds run by a method): no subagents, or subagents lacking a round's tools (`/deep-research` round: web search and fetch; ultracode round: file reading) → skip that round, list it under Gaps, and go on to the other round or step 6.
   - **Report progress** at each round's end:
     [agent-pm-progress:round] the round and its agent count against its cap
6. **Failure.** Never retry or replace a run. Usable findings (supported refutations count) → report. None → `failed`, stop.
7. **Report** (Researcher › Standards). Revising a Light Research report (its `Light Research.` line marks it, in any language) → drop that line.
8. **Finish.** `status: done`; `summary` 3–5 lines (Researcher › Standards); for local or mixed, one line per round run: `<round>: <n>/<cap> agents`; a braked round: `<round>: skipped` with the gate's output, `<n>` the distinct agents that round's run records (beside its Script file) show as started; for a round run by a method, `<n>` the subagents it dispatched (no session files read), `<cap>` 100 (deep-research method) or the budget's cap (ultracode method).

## Resume

The prompt starts "Resumed agent run" → re-read the input (it may have changed). Do only what is missing; never redo finished work.

- **What finished**: each Workflow call this session reported a Run ID and a Script file; that run's records (its status, each agent's result) sit in this session's directory beside the script. A round is completed only if its records say so.
- **Limits stand**: the budget reported this session binds: no new round, no higher cap. A re-run that fills a gap replaces its agent, uncounted. A brake-skipped round stays skipped; a planned round never started brakes first if it is second; finishing an interrupted round needs no brake.
- **Each round**:
  - Completed → use its result as is; give each claim left unverified (< 2 valid votes) 3 fresh votes.
  - Interrupted ultracode round → call the Workflow with its Script file as `scriptPath`, the same `args` and its Run ID as `resumeFromRunId`.
  - Interrupted `/deep-research` round → never pass `resumeFromRunId` (it re-runs nearly everything). Stopped before its search finished → call it once more with the same `args`. Otherwise finish only its missing fetch and verify work with Agent calls (≤ 10 at a time), reusing every result that came back, by the script's own prompts and limits.
  - Never started → step 5.
- **Same standards**: step 5's voting rule and privacy filter apply to every re-run; local or mixed prepares again first.

Then steps 7–8. Nothing usable → `failed`.
