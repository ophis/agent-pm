# TASK-44: Role + task restructure (phase 1) — plan

RESUME: phase=S3 worktree=/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task branch=TASK-44-role-task base_ref=db150ef8c9febd7f265d45d43f10ee2e7c31ce8f review_round=0 spec_file=docs/specs/2026-09-28-task-44-role-task-design.md

## Implementation plan

(S4)

## Progress

- S1: worktree created from origin/main (db150ef).
- decision(validation scope): runnable() validates every declared role/task, not only referenced ones - surfaces registry errors at deploy (NFR-2); dissent: none
- decision(allowed_tools check location): stays in load_config, now over [tasks] - keeps today's behavior for every consumer; dissent: none
- decision(charter memory section): heading present, body empty (FR-2a literal); dissent: none
