# TASK-44: Role and task file pairs (change request) — plan

RESUME: phase=S4 worktree=/Users/francis/playground/agent-pm/work/TASK-44/worktrees/TASK-44-role-task branch=TASK-44-role-task base_ref=124a7b7802c1e68922489d61d478398870853632 review_round=2 spec_file=docs/specs/2026-09-29-task-44-role-task-pairs-design.md

## Implementation plan

(S4)

## Progress

- S1: reusing the existing worktree on TASK-44-role-task (base 124a7b7, the converged phase-1 build).
- decision(pair scope): validate every .md/.toml pair found in roles/ and tasks/, not only referenced ones - orphans must fail at deploy; dissent: none
- decision(check location): role/task checks stay in runnable(); promote (load_config only) keeps working; dissent: none
- decision(launcher memory-vs-repo scope): applies to every role with memory on a repo_from_issue task, not only read_only {repo} - memory in the target clone would leak into commits; dissent: none
- S2: spec written (docs/specs/2026-09-29-task-44-role-task-pairs-design.md).
- S3 panel: core=[architecture,spec-fitness] +optional=[security] (run permissions: memory dir vs deny rules)
- S3 reviewers: architecture=a30de7fbba7481ada spec-fitness=a94117bc5e79e89d1 security=ae2ba65ff1ccbdfa8
- S3 r0: architecture=PASS spec-fitness=PASS security=FAIL -> security#1 'deny rules cover the .toml files' is false: Bash writes bypass Edit denies and roles/ tasks/ are add-dirs, so run-permission settings become run-writable; fix: launcher refuses (exit 2) while git status shows changes under roles/ or tasks/, claim corrected, residual stated
- S3 r1: architecture=FAIL spec-fitness=PASS security=PASS -> architecture#6 registry guard in the launcher runs after the claim, so a dirty roles/ (e.g. .DS_Store) burns every queued issue's attempts; fix: guard moves to the router tick before Recover/claim, counts tracked changes + untracked/ignored .md/.toml only
- S3 r2: architecture=PASS spec-fitness=PASS security=PASS -> converged. Deferred NBs: architecture#5 hardcoded error prefixes (test-only); security#3 ~/.local/bin not protected; security#4 task key types unchecked (as phase 1). Plan-level NBs: guard scoped to tick; filter applies to untracked/ignored only; run router dry-run after committing.
