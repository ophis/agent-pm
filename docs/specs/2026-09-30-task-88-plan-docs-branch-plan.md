# TASK-88 plan: plan_docs only for this branch, trim eng.py

RESUME: phase=S3 worktree=/Users/francis/playground/agent-pm/work/TASK-88/worktrees/TASK-88-eng-py-plan-docs branch=TASK-88-eng-py-plan-docs base_ref=452ce156fd3d640967545a4f849322d562e9e5be review_round=0 spec_file=/Users/francis/playground/agent-pm/work/TASK-88/worktrees/TASK-88-eng-py-plan-docs/docs/specs/2026-09-30-task-88-plan-docs-branch-design.md

## Progress

- S1: worktree created on origin/main (452ce15).
- S2: spec written; decisions solo (PRD prescriptive).
- decision(errors): keep the Malformed checks, re-typed as TransientError - they guard gh-login identity; dissent: none
- decision(resolve-raises tests): delete now-invalid Transient cases instead of asserting raises - callers' conversion already tested; dissent: none
