# TASK-67: role identity — plan

RESUME: phase=S3 worktree=/Users/francis/playground/agent-pm/work/TASK-67/worktrees/TASK-67-role-identity-per-role-config-and-linear branch=TASK-67-role-identity-per-role-config-and-linear base_ref=dd157f7631f98fd5a3c9070cef9c036d85ab8201 review_round=0 spec_file=docs/specs/2026-09-29-task-67-role-identity-design.md

## Implementation plan

(S4)

## Progress

- decision(FR-15): no code change - the prompt already passes the resolved task file; Reviewer: stays until PR-B; dissent: none
- decision(attempt cap): role accounts join the agent set in Router's by_user check (G5) over leaving it to PR-C's FR-11 - a role session's own move to Todo would otherwise reset the cap forever, breaking "runs as before"; dissent: none
- decision(project task in role tasks): runnable() rejects a project task outside its role's tasks - gives `tasks` its meaning while dispatch is by project; dissent: none
- decision(harness key source): linear_gql reads harness_key from pipeline.toml per call over threading cfg through every caller - no caller signature changes; dissent: none
