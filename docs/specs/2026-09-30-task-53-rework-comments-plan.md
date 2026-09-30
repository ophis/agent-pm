# TASK-53: rework comments since the latest Build started — plan

Spec: `docs/specs/2026-09-30-task-53-rework-comments-design.md`

RESUME: phase=S3 worktree=/Users/francis/playground/agent-pm/work/TASK-53/worktrees/TASK-53-build-started-pr branch=TASK-53-build-started-pr base_ref=8b826cacafcca2445a286c74fd827d1705bba93b review_round=1 spec_file=/Users/francis/playground/agent-pm/work/TASK-53/worktrees/TASK-53-build-started-pr/docs/specs/2026-09-30-task-53-rework-comments-design.md

## Progress

- S1: worktree created from origin/main (8b826ca).
- S2: spec written.
- decision(cutoff): eng.py computes it from Linear over the agent passing --since - acceptance tests need it in code; dissent: none
- decision(trigger): only user comments start a new build, others ride along as review input - issue's recommendation, avoids bot loops; dissent: none
- decision(output): one time-ordered `user` list (Linear + PR) and `others` (PR) - precedence is newer-over-older across both; dissent: none
- decision(marker): Build started = non-human author + body prefix - a human's words must not reset the window; dissent: none
- S3 panel: core=[architecture,spec-fitness] +optional=[security] transport=Workflow
- S3 r0: architecture=PASS spec-fitness=PASS security=FAIL -> security#1 marker spoofable by any non-human author; security#2 others' bodies need delimited untrusted blocks, never verbatim into spec/plan; fixed in spec (marker = engineering role account; step 6 block rule; plus NB: exit 2 on broken registry, explicit params, earlier comments are context, tie/`at == since` test, residuals listed)

## Implementation plan

(S4)
