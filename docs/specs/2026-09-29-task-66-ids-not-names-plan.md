# TASK-66: reference the Linear team and workflow states by id — plan

RESUME: phase=S3 worktree=/Users/francis/playground/agent-pm/work/TASK-66/worktrees/TASK-66-reference-the-linear-team-and-workflow-s branch=TASK-66-reference-the-linear-team-and-workflow-s base_ref=b5473cec0fe9b27b79d319dfa6ea9ac350197d01 review_round=0 spec_file=/Users/francis/playground/agent-pm/work/TASK-66/worktrees/TASK-66-reference-the-linear-team-and-workflow-s/docs/specs/2026-09-29-task-66-ids-not-names-design.md

## Implementation plan

(S4)

## Progress

- decision(path): architectural (config interface across router/promote/prune/launcher); spec in docs/specs per CLAUDE.md; solo.
- decision(validation site): offline shape check in load_config + one `team()` Linear query in router/promote/prune, launcher only passes ids on - router validated the same file this tick; dissent: none.
- decision(team in prompt): tail also passes `Team: <id>` so principles.md's team line is rename-proof like the states; solo.
