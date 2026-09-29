# TASK-44: Rules by role (round 3) — plan

RESUME: phase=S4 worktree=/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task branch=TASK-44-role-task base_ref=0e1a758504afbee39131b6f091256c5f2c4829ba review_round=1 spec_file=docs/specs/2026-09-29-task-44-rules-by-role-design.md

## Implementation plan

(S4)

## Progress

- S1: reusing the existing worktree on TASK-44-role-task (base 0e1a758, the converged pairs build).
- decision(shared-rule scope): principles rules state their own scope (private_docs documents; "when your task's check finds…") so engineering is unaffected; tasks keep their own too-vague criteria and commit messages; dissent: none
- decision(precedence wording): "outranks the charter" (was "the charter's boundaries") since researcher/pm rules are standards; dissent: none
- decision(G1 method): git revert b26e99d (self-contained; no later scripts/ change) + doc edits; dissent: none
- S2: spec written (docs/specs/2026-09-29-task-44-rules-by-role-design.md).
- S3 panel: core=[architecture,spec-fitness] +adhoc=[instruction-fidelity] (security dropped: only a URL matched; the guard removal is the user's decision)
- S3 reviewers: architecture=aa2e60373082f083a spec-fitness=a8cb02c8b3228e94b instruction-fidelity=aa6815d2c0bfd0cf1
- S3 r0: architecture=PASS spec-fitness=PASS instruction-fidelity=FAIL -> instruction-fidelity#1 engineer "not written by the user" widens eng.py `kept` exception; instruction-fidelity#2 charter rules repeated by template section notes (research-report 缺口/更正, prd.md 假设/已完成); fix: spec edited (snapshot .r1.md)
- S3 r1: architecture=PASS spec-fitness=PASS instruction-fidelity=PASS -> converged. Applied after: instruction-fidelity#9 (conventions scope: code, docs, commits), spec-fitness#6 (step 6 "copied into the requirement"), G2 note dropped (instruction-fidelity#10). Deferred NBs: instruction-fidelity#6 synthesis loses "from the single run"; instruction-fidelity#10 no `Build started` comment case left as the user worded it; architecture#6 heading rename touches charters; spec-fitness#4 private_docs-only publish scope.
