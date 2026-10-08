---
description: "Quick research on a question about the web, a codebase, or both: one round of parallel agents and a short sourced Markdown report. Use when a fast, good-enough answer will do; for verified depth use researcher-deep-research."
---

# Light Research

Run one round of parallel agents, then write a short report.

## Steps

1. **Read** the input; decide its type (Researcher › Type and target).
2. **Too vague** (Output): no clear question, scope, deliverable or, for local or mixed, target → `needs_input`, stop.
3. **Prepare** (local, mixed): Researcher › Type and target.
4. **Report progress:**
   [agent-pm-progress:start] the type, plus each target repo and its commit for local or mixed
5. **Research.** Split the question into 3–6 **angles**; dispatch one subagent per angle, all in parallel, each restricted per Researcher › Type and target.
6. **Failure.** Never retry or replace an agent. Some usable findings → report, failed angles under Gaps. None → `failed`, stop.
7. **Report** (Researcher › Standards), after the type line `Light Research. Angles: <angle 1>; <angle 2>; …. No independent verification stage: each finding is checked only by the agent that found it.`, ≤ 3000 words.
8. **Finish.** `status: done`; `summary` 3–5 lines (Researcher › Standards). One round can't settle the question → end `summary` with `Suggest upgrading to Deep Research: <reason>`.

## Resume

The prompt starts "Resumed agent run" → re-read the input (it may have changed); reuse this session's results. This overrides steps 3–6:
- No agent dispatched this session → steps 3–8.
- Report not written → prepare again (local, mixed), then re-dispatch each angle without a result (none, or an error) with its original prompt, once, in one parallel batch.

Then steps 7–8. Still nothing usable → `failed`.
