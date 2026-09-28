# TASK-44: Role + task restructure (phase 1) — plan

RESUME: phase=S4 worktree=/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task branch=TASK-44-role-task base_ref=db150ef8c9febd7f265d45d43f10ee2e7c31ce8f review_round=1 spec_file=docs/specs/2026-09-28-task-44-role-task-design.md

## Implementation plan

(S4)

## Progress

- S1: worktree created from origin/main (db150ef).
- decision(validation scope): runnable() validates every declared role/task, not only referenced ones - surfaces registry errors at deploy (NFR-2); dissent: none
- decision(allowed_tools check location): stays in load_config, now over [tasks] - keeps today's behavior for every consumer; dissent: none
- decision(charter memory section): heading present, body empty (FR-2a literal); dissent: none
- S2: spec written (docs/specs/2026-09-28-task-44-role-task-design.md).
- S3 panel: core=[architecture,spec-fitness] +optional=[security]
- S3 reviewers: architecture=abddcf6a9cc330698 spec-fitness=ae1ff96f3f1a26822 security=a86d36e6dafb2b314
- S3 r0: architecture=PASS spec-fitness=PASS security=FAIL -> security#1 memory may overlap ROOT/ancestors (writable runner); fix: memory not at/under ROOT nor ancestor of ROOT or read_only; also applied NBs: normalized read_only, unknown-key check, Run carries resolved paths, repo_step returns result, one-commit/NFR-3/post-deploy notes
- S3 r1: architecture=PASS spec-fitness=PASS security=PASS -> converged. Applied NBs: resume wording; memory also not at/under ~/.claude or ~/Library/LaunchAgents. Deferred NBs: security#3 symlinked read_only, #4 add_dirs overlap, #5 allowed_tools vs read_only (all unchanged from today), #6 memory as injection channel (TASK-33).
