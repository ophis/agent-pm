# TASK-88 plan: plan_docs only for this branch, trim eng.py

RESUME: phase=S9 worktree=/Users/francis/playground/agent-pm/work/TASK-88/worktrees/TASK-88-eng-py-plan-docs branch=TASK-88-eng-py-plan-docs base_ref=452ce156fd3d640967545a4f849322d562e9e5be review_round=0 spec_file=/Users/francis/playground/agent-pm/work/TASK-88/worktrees/TASK-88-eng-py-plan-docs/docs/specs/2026-09-30-task-88-plan-docs-branch-design.md

## Progress

- S1: worktree created on origin/main (452ce15).
- S2: spec written; decisions solo (PRD prescriptive).
- decision(errors): keep the Malformed checks, re-typed as TransientError - they guard gh-login identity; dissent: none
- decision(resolve-raises tests): delete now-invalid Transient cases instead of asserting raises - callers' conversion already tested; dissent: none
- S3 panel: core=[architecture,spec-fitness] +optional=[security] transport=Workflow
- S3 r0: architecture=PASS spec-fitness=PASS security=PASS -> converged; clarified spec from non-blockers (os.sep prefix test, handled-failure wording, accepted limits)
- S4: task list written.

## Implementation plan

Spec: `docs/specs/2026-09-30-task-88-plan-docs-branch-design.md` (same dir).

Global constraints:
- Worktree `/Users/francis/playground/agent-pm/work/TASK-88/worktrees/TASK-88-eng-py-plan-docs`, branch `TASK-88-eng-py-plan-docs`; absolute paths / `git -C <worktree>`; before any write assert `git -C <worktree> branch --show-current` is `TASK-88-eng-py-plan-docs`. Never touch `main`, never force-push or merge.
- Verify: `python3 -m unittest discover -s scripts/tests` (run from the worktree) — all pass.
- After each task's commit run exactly `git -C /Users/francis/playground/agent-pm/work/TASK-88/worktrees/TASK-88-eng-py-plan-docs push -u origin TASK-88-eng-py-plan-docs`.
- Follow the repo's `CLAUDE.md`: terse code, no comments unless a gotcha. Spec section E (kept checks) is off limits. No new features. `status`/`comments` JSON fields change only as the spec says.

### Task 1: plan_docs only for this branch

Files: `scripts/eng.py` (`_plan_docs`, `cmd_status`), `scripts/tests/test_eng.py`.
Produces: `_plan_docs(worktree, branch)`.
Tests first: add `test_status_plan_docs_only_this_branch` (spec "Tests" bullet 2) and run it to see it fail; add `branch=TASK-26-x` to the `RESUME:` fixtures of `test_status_json` and `test_status_skips_unreadable_plan_docs`. Edge cases in the new test: another issue's `phase=S9` doc, `branch=TASK-26-xy` (prefix trap), no `branch=` token.
Commit: `TASK-88: plan_docs lists only this branch's plan docs`

### Task 2: one failure path, single-page Linear comments, non-human marker

Files: `scripts/eng.py` (`Malformed`, `TransientError` docstring, `_json`, `_rows`, `_login`, `_pr`, `_pr_entries`, `Q_COMMENTS`, `Note`, `_note`, `_linear`, `iso`, `cmd_comments`, `_command`, `main`, imports), `scripts/tests/test_eng.py`.
Spec: B, C, D rows 4 and 9.
Produces: `main(argv, env, gql, run, out, err, config)` (no `root`); `cmd_comments(ok, run, out, gql, humans)`; every handled failure → `eng.py: <reason>` on stderr, exit 1.
Tests: exit-code tests expect 1 and `eng.py: <reason>` (rename `…_exits_2`/`…_exits_3`); extend `test_since_is_the_latest_engineer_build_started` with a human `Build started` after the latest marker (stays in `user`, `since` unchanged); `linear_for` single page; delete `:383-395`, `:436-445`, `:488-495`, `:513-537`, `:539-547` and any import left unused.
Commit: `TASK-88: one failure path (exit 1), single-page Linear comments, non-human Build started marker`

### Task 3: resolve simplifications

Files: `scripts/eng.py` (`_call`, `_repo_info`, `_existing_branches`, `_project_id`, `resolve`, `Ok`, `_branch_exists`, `worktrees_dir_ok`, `cmd_status`), `scripts/tests/test_eng.py`.
Spec: D rows 3, 5, 6, 7, 8, 10.
Produces: `Ok(..., mapped=False, branch_exists=None)`; `status` without `worktrees_dir_ok`, `branch_exists` from `ok.branch_exists`.
Tests: `Resolve` asserts `branch_exists` for local, remote and new branches; new `Resolve` test for a symlinked-out `work/TASK-26/worktrees` (that `Invalid`, no `run` calls) replacing `test_status_worktrees_dir_ok_false_for_symlink_out`; merge the four `project mapping` tests (`:254-289`) into one; drop the `wt`/`rev-parse`, `show_ref`, `run`-exception and malformed-repo-JSON `Transient` cases and the non-dict/list `project` cases; the `Cli` table loses `show_ref`/`remote` and `self.ok` sets `branch_exists="local"` where `test_status_json` needs it.
Commit: `TASK-88: trim resolve: no _call, branch_exists in Ok, worktrees check in resolve`

### Task 4: tasks/engineering.md

Files: `tasks/engineering.md`.
Spec: F.
Tests: none (doc); the verify command still passes.
Commit: `TASK-88: engineering.md: field names only, this branch's plan docs, dedupe`
- S5: Tasks 1-4 done via SDD (db5d137, 986d24b, 35c4148, 2b5763b), each task review clean; minors deferred to S7.
- S6: suite 305 OK; new test fails on 452ce15 (Task 1 test file vs old eng.py); live smoke: status/comments exit 0, no worktrees_dir_ok, AGENT_PM_ISSUE unset -> exit 1; _plan_docs on real worktree lists only this branch.
- S7 panel: core=[correctness,requirement-fidelity,doc] +optional=[code-quality,test] transport=Workflow (dropped architecture, performance: pure simplification)
- S7 r0: correctness=PASS requirement-fidelity=PASS doc=PASS code-quality=PASS test=PASS -> converged
- S8: skipped (keep the commits, per the run's requirement).
- S9: residual non-blocking: Linear node fields (`body`, `user`) read outside `_linear`'s wrapper, so a malformed node gives a traceback (still exit 1) and the `_linear` docstring overstates; no test left for the `eng.py: Linear: …` line or the body-rule negatives (PRD-mandated deletions); `_existing_branches` docstring run-on; `os.path.join(base, "worktrees")` repeated in `resolve`; engineering.md no longer defines `since`; a plan doc without `branch=` counts as no build.
