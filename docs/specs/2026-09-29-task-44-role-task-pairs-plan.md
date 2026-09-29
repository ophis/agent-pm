# TASK-44: Role and task file pairs (change request) — plan

RESUME: phase=S3 worktree=/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task branch=TASK-44-role-task base_ref=124a7b7802c1e68922489d61d478398870853632 review_round=0 spec_file=docs/specs/2026-09-29-task-44-role-task-pairs-design.md

## Implementation plan

(S4)

## Progress

- S1: reusing the existing worktree on TASK-44-role-task (base 124a7b7, the converged phase-1 build).
- decision(pair scope): validate every .md/.toml pair found in roles/ and tasks/, not only referenced ones - orphans must fail at deploy; dissent: none
- decision(check location): role/task checks stay in runnable(); promote (load_config only) keeps working; dissent: none
- decision(launcher memory-vs-repo scope): applies to every role with memory on a repo_from_issue task, not only read_only {repo} - memory in the target clone would leak into commits; dissent: none
- S2: spec written (docs/specs/2026-09-29-task-44-role-task-pairs-design.md).
