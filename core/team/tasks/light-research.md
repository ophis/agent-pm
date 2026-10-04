# Light Research

Run one round of parallel agents, then write a short report.

## Steps

1. **Read** the input; decide its type (Researcher › Type and target).
2. **Too vague** (Output): no clear question, scope, deliverable or, for local or mixed, target (Researcher › Type and target) → `needs_input`, stop.
3. **Report start.**
   [agent-pm-progress:start] the type, plus the target repo for local or mixed
4. **Research.**
   - Local or mixed: prepare (Researcher › Type and target).
   - Split the question into 3–6 **angles** by importance, from the input: web angles for web, worktree angles for local, both for mixed. File each known claim under its angle as a claim to verify.
   - Dispatch one agent per angle, as parallel foreground Agent calls in one message. Each prompt is self-contained: the angle's questions, shared context, claims to verify and the agent's restriction (Researcher › Type and target). It asks for primary sources and, per finding, the claim, its sources (`<path from the worktree root>:<a>-<b>` for code), whether a source states it directly, how many independent sources back it, and confidence; plus what it couldn't cover.
   - One round: no Workflow, verification stage or follow-up. Aim for ~10 minutes.
5. **Failure.** Never retry or replace an agent. Some usable findings → report, listing failed angles under Gaps. None → `failed`, stop.
6. **Report** (Researcher › Standards), the line after the title `Light Research. Angles: <angle 1>; <angle 2>; …. No independent verification stage: each finding is checked only by the agent that found it.`, ≤ 3000 words, Conclusion and recommendation one paragraph of ≤ 5 sentences.
7. **Finish.** `status: done`; `summary` 3–5 lines (Researcher › Standards). If one round can't settle the question (core gaps, single-source key claims, conflicting sources), end `summary` with `Suggest upgrading to Deep Research: <reason>`.

## Resume

The prompt starts "Resumed run" → re-read the input (it may have changed) and reuse everything this session produced. This overrides steps 4–5:
- No agent dispatched this session → run steps 3–7 as usual.
- Report not written → keep the results you have; prepare again (local, mixed), then re-dispatch each angle without a result (none, or an error) with its original prompt, once, in one parallel batch.

Then finish steps 6–7. Still nothing usable → `failed`.
