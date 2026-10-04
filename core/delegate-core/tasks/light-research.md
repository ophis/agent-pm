# Light Research

Run one round of parallel agents, then write a short report.

## Steps

1. **Read** the input; decide its type (Researcher › Type and target).
2. **Too vague** (Output): no clear question, scope, deliverable or, for local or mixed, target (Researcher › Type and target) → `needs_input`, stop.
3. **Research.**
   - Local or mixed: prepare (Researcher › Type and target).
   - Split the question into 3–6 **angles** by importance, from the input: web angles for web, worktree angles for local, both for mixed. File each known claim under its angle as a claim to verify.
   - Dispatch one agent per angle, as parallel foreground Agent calls in one message. Each prompt is self-contained: the angle's questions, shared context, claims to verify and the agent's restriction (Researcher › Type and target). It asks for primary sources and, per finding, the claim, its sources (`path:line` for code), whether a source states it directly, how many independent sources back it, and confidence; plus what it couldn't cover.
   - One round: no Workflow, verification stage or follow-up. Aim for ~10 minutes and ≤ 10% of the 5-hour usage window.
4. **Failure.** Never retry or replace an agent. Some usable findings → report, listing failed angles under 缺口. None → `failed`, stop.
5. **Report** (Researcher › Standards), first line `轻量调研（Light Research）。角度：<angle 1>；<angle 2>；…。没有独立核实阶段：每条结论只经检索 agent 自行核实。`, ≤ 3000 字, 结论与建议 one paragraph of ≤ 5 points.
6. **Finish.** `status: done`; `summary` 3–5 lines (Researcher › Standards). If one round can't settle the question (core gaps, single-source key claims, conflicting sources), add `建议升级为 Deep Research` and why.

## Resume

The prompt starts "Resumed run" → re-read the input (it may have changed) and reuse everything this session produced. This overrides steps 3–4:
- No agent dispatched this session → step 3.
- Report not written → keep the results you have; prepare again (local, mixed), then re-dispatch each angle without a result (none, or an error) with its original prompt, once, in one parallel batch.

Then finish steps 5–6. Still nothing usable → `failed`.
