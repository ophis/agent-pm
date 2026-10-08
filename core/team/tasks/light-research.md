---
description: "Quick research on a question about the web, a codebase, or both: one round of parallel agents and a short sourced Markdown report. Use when a fast, good-enough answer will do; for verified depth use researcher deep-research."
---

# Light Research

Run one round of parallel agents, then write a short report.

## Steps

1. **Input** (Researcher › Input).
2. **Prepare** (local, mixed): Researcher › Type and target.
3. **Report progress:**
   [agent-pm-progress:start] the type, plus each target repo and its commit for local or mixed
4. **Research.** 3–6 **angles** (web for web, worktree for local, both for mixed), one subagent each, all in parallel, restricted per Researcher › Type and target; each checks its own findings and returns, per finding, the claim, its sources, how many are independent, and confidence; plus what it could not cover.
5. **Failure** (Researcher › Failure). Failed angles go under Gaps.
6. **Report** (Researcher › Standards), after the type line `Light Research. Angles: <angle 1>; <angle 2>; …. No independent verification stage: each finding is checked only by the agent that found it.`, ≤ 3000 words.
7. **Finish.** `status: done`. One round can't settle the question (core gaps, single-source key claims, conflicting sources) → end `summary` with `Suggest upgrading to Deep Research: <reason>`.

## Resume

Reuse this session's results. Nothing dispatched → step 2. Report not written → re-dispatch each angle without a result (none, or an error) with its original prompt, once, in one parallel batch. Then steps 6–7; still nothing usable → `failed`.
