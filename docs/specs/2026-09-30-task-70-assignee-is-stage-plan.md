# TASK-70: the assignee is the stage — plan

RESUME: phase=S4 worktree=/Users/francis/playground/agent-pm/work/TASK-70/worktrees/TASK-70-the-assignee-is-the-stage-claim-recover branch=TASK-70-the-assignee-is-the-stage-claim-recover base_ref=be21167bcace11ed397c373c30f40da188817c21 review_round=0 spec_file=docs/specs/2026-09-30-task-70-assignee-is-stage-design.md

## Implementation plan

(S4)

## Progress
- decision(launcher input): chose `--assignee <email>` over `--role` - FR-13 literal; router resolves the same accounts by id, so the launcher match cannot miss; dissent: ops lens preferred `--role` (fail before claim).
- decision(account matching): chose resolving role accounts to user ids (`role_ids`, stop on unknown) over `email in` filter - Linear `in` is case-sensitive, promote needs ids; dissent: none.
- decision(child id key): chose next role name over the source's project id - project no longer distinguishes the hop; dissent: ops lens preferred project id (stable against toml edits/reassign).
- decision(deep-research step 4 / task Board bullets): drop "assignee viewer" and "The <stage> project" - FR-8 and the stage-project model they encode; dissent: none.
- S3 panel: core=[architecture,spec-fitness] +optional=[] (security dropped: keys/launcher identity unchanged) transport=Workflow
- S3 r0: architecture=PASS spec-fitness=PASS -> converged; folded NB: load_config docstring, CLAUDE.md promote validates registry
