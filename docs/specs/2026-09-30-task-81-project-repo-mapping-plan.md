# TASK-81: project → repo mapping — plan

RESUME: phase=S3 worktree=/Users/francis/playground/agent-pm/work/TASK-81/worktrees/TASK-81-build branch=TASK-81-build base_ref=0651ba0e2469f8607ff782b9a0001a9c10da8736 review_round=0 spec_file=docs/specs/2026-09-30-task-81-project-repo-mapping-design.md

## Progress

- S1: reused the existing worktree (branch TASK-81-build at origin/main 0651ba0).
- S2: spec written (architectural: config schema + `resolve` interface).
- decision(mapping plumbing): chose a `repos` dict argument to `resolve` + `Ok.mapped` flag over resolve loading config itself - PRD FR-3 says the caller passes it; the flag is the launcher's only way to annotate; dissent: none.
- decision(prefix wording): chose `project mapping ` + today's `<owner>/<name>: …` reason over a second copy of the repo - satisfies FR-6's prefix without repetition; dissent: none.
- decision(CLI config error): chose exit 2 (`eng.py: <msg>`) over an uncaught SystemExit (exit 1) - keeps the documented exit codes; dissent: none.
