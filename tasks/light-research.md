# Light Research

Run one round of parallel agents, then write a short report.

## Steps

1. **Read** the named issue (none → stop) and its comments; decide its type (charter).
2. **Too vague** (principles): no clear question, scope, deliverable or, for local or mixed, target (charter).
3. **Claim.** Unless the runner claimed it: re-read the status; not Todo → stop; else set In Progress. Comment that research started.
4. **Research.**
   - Local or mixed: prepare (charter).
   - Split the question into 3–6 **angles** by importance, from the description and the user's comments: web angles for web, worktree angles for local, both for mixed. File each known claim under its angle as a claim to verify.
   - Dispatch one agent per angle, as parallel foreground Agent calls in one message. Each prompt is self-contained: the angle's questions, shared context, claims to verify and the agent's restriction (charter). It asks for primary sources and, per finding, the claim, its sources (`path:line` for code), whether a source states it directly, how many independent sources back it, and confidence; plus what it couldn't cover.
   - One round: no Workflow, verification stage or follow-up. Aim for ~10 minutes and ≤ 10% of the 5-hour usage window.
5. **Failure.** Never retry or replace an agent. Some usable findings → publish, listing failed angles under 缺口. None → comment the failure, move to Todo, stop.
6. **Report** (charter), first line `轻量调研（Light Research）。角度：<angle 1>；<angle 2>；…。没有独立核实阶段：每条结论只经检索 agent 自行核实。`, ≤ 3000 字, 结论与建议 one paragraph of ≤ 5 points.
7. **Hand off.** Comment a 3–5 line summary (charter) with the link. If a Light run can't settle the question (core gaps, single-source key claims, conflicting sources), add `建议升级为 Deep Research` and why. Set In Review; end with the link.

## Resume

Overrides steps 4–5:
- No agent dispatched yet → step 4.
- Report not committed → keep the results you have; prepare again (local, mixed), then re-dispatch each angle without a result (none, or an error) with its original prompt, once, in one parallel batch.

Then finish what's missing of steps 6–7: report published (principles), hand-off comment, In Review. Still nothing usable → comment what failed, set In Review.
