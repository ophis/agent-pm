# TASK-53: rework comments since the latest Build started — plan

Spec: `docs/specs/2026-09-30-task-53-rework-comments-design.md`

RESUME: phase=S9 worktree=/Users/francis/playground/agent-pm/work/TASK-53/worktrees/TASK-53-build-started-pr branch=TASK-53-build-started-pr base_ref=8b826cacafcca2445a286c74fd827d1705bba93b review_round=0 spec_file=/Users/francis/playground/agent-pm/work/TASK-53/worktrees/TASK-53-build-started-pr/docs/specs/2026-09-30-task-53-rework-comments-design.md

## Progress

- S1: worktree created from origin/main (8b826ca).
- S2: spec written.
- decision(cutoff): eng.py computes it from Linear over the agent passing --since - acceptance tests need it in code; dissent: none
- decision(trigger): only user comments start a new build, others ride along as review input - issue's recommendation, avoids bot loops; dissent: none
- decision(output): one time-ordered `user` list (Linear + PR) and `others` (PR) - precedence is newer-over-older across both; dissent: none
- decision(marker): Build started = non-human author + body prefix - a human's words must not reset the window; dissent: none
- S3 panel: core=[architecture,spec-fitness] +optional=[security] transport=Workflow
- S3 r0: architecture=PASS spec-fitness=PASS security=FAIL -> security#1 marker spoofable by any non-human author; security#2 others' bodies need delimited untrusted blocks, never verbatim into spec/plan; fixed in spec (marker = engineering role account; step 6 block rule; plus NB: exit 2 on broken registry, explicit params, earlier comments are context, tie/`at == since` test, residuals listed)
- decision(marker, revised at S3 r0): Build started = engineering role account + body prefix - only the posting account may reset the window; dissent: none
- S3 r1: architecture=PASS spec-fitness=PASS security=PASS -> converged. NB kept: security#4 no size cap on others (residual), security#6 harness key for Linear reads (as resolve); architecture: registry lookup stays in main, only the email set passed down
- S4: task list written
- S5: Task 1 da214b9, Task 2 7abc6c2; per-task reviews approved. Deferred minors: human_members built for every command outside try (non-string entry -> traceback); `others` ordering untested alone; null PR body untested; engineering.md Inputs window/`since` wording, step 4 repeats user/others clause
- S6: `python3 -m unittest discover -s scripts/tests` 313 OK; live `AGENT_PM_ISSUE=TASK-53 eng.py comments` -> since = this run's Build started, `--since` refused
- S7 panel: core=[correctness,requirement-fidelity,doc] +optional=[code-quality,test,security] transport=Workflow (performance, architecture dropped: small CLI change, architecture reviewed at S3; security added: untrusted-comment rules)
- S7 r0: correctness=PASS requirement-fidelity=PASS doc=PASS code-quality=PASS test=PASS security=PASS -> converged
- S8: skipped (keep the commits)
- Residual non-blocking: human_members unvalidated (non-string entry -> traceback, also on status); empty marker set silently falls back to issue creation; Linear shape errors exit 3 not 2; step 1 doesn't say where earlier comments are read; step 4 resume wording could read as others driving spec edits (step 6 governs); step 4/1/6 repeat the others rule; `others` sort and null PR body untested; tests take marker accounts from the real roles/ registry; _command param count, Note placement; no size cap on others; harness key for Linear reads

## Implementation plan

Spec: `docs/specs/2026-09-30-task-53-rework-comments-design.md`

### Global Constraints

- Verify: `python3 -m unittest discover -s scripts/tests` (no network, Keychain or Claude); all green.
- Match the file's style: dense one-line helpers, few comments, frozen dataclasses, exit codes 0/2/3 as today.
- Marker accounts: `{r.account.lower() for r in registry(root)[0].values() if "engineering" in r.tasks}`, computed in `main` only for `comments`; `registry` `SystemExit` → `eng.py: <msg>`, exit 2.
- Output keys exactly: `since`, `user`, `others`; entry keys `at`, `source`, `kind`, `author`, `body` (+ `state` for `review`, `path`/`line` for `review_comment`).

### Task 1: `eng.py comments` since the latest Build started, user vs others

Files: `scripts/eng.py`, `scripts/tests/test_eng.py`.

Consumes: `pipeline.registry`, `pipeline.ROOT`, `load_config(...)["human_members"]`, existing `_pr`, `_pages`, `_login`, `iso`.

Produces:
- `Q_COMMENTS`: `issue(id: $i) { createdAt comments(first: 250, after: $c) { nodes { body createdAt user { email } } pageInfo { hasNextPage endCursor } } }`.
- `cmd_comments(ok, run, out, gql, humans, markers)` (`humans`, `markers`: sets of lowercase emails); `_command(a, issue, cfg, markers, gql, run, out, err)` or equivalent explicit params; `main(argv, env, gql, run, out, err, config=CONFIG, root=ROOT)`; `comments` subcommand takes no arguments (`--since` refused by argparse, exit 2).
- Linear fetch paginated; any Linear exception or missing field → `TransientError("Linear: …")` (exit 3).
- `since` = max `createdAt` of comments whose stripped body matches `Build started\b` and whose author email (lowercase) is in `markers`, else issue `createdAt`; printed as `since.isoformat()`.
- `user`: Linear comments by `humans` + PR rows by the `gh` login, `at > since`, sorted by instant. `others`: PR rows by any other author (`author` null if no user). Kinds: issues comments → `comment`, reviews → `review` (+`state`), pulls comments → `review_comment` (+`path`, `line` = `line` else `original_line`). Pending reviews skipped; bad `gh` timestamp → `Malformed`.
- No PR → Linear-only entries, no `--paginate` calls.

Tests first (fake `gql` returning pages; `FakeRun`):
- latest of several engineer-account `Build started` comments is the cutoff; Linear and PR entries before it excluded, after included;
- no marker → `since` = issue `createdAt`; a PR row older than it excluded;
- `Build started` by a human (a `user` entry), by the pm account, and with `user: null` do not move the cutoff;
- entry at exactly `since` excluded; time comparison across offsets;
- three PR kinds with `kind`, `state`, `path`/`line` (incl. `original_line` fallback);
- other authors in `others`, separate from `user`; Linear non-human comments in neither;
- Linear pages all read (second page holds the marker); `gh` pages all read; pending review skipped;
- no PR → Linear-only, no paginate call;
- Linear raise / missing field → exit 3; broken registry → exit 2; `--since` refused; existing gh failure (3) and malformed (2) cases updated to the new argv.

Commit: `TASK-53: eng.py comments reads since the latest Build started, user and others apart`

### Task 2: Engineering task, README and docstring

Files: `tasks/engineering.md`, `README.md`, `scripts/eng.py` (module docstring only if it names the old spec for `comments`).

Consumes: Task 1's output shape.

Produces:
- `tasks/engineering.md` Inputs: `<eng> comments` (no args) output described (`since`, `user`, `others`, entry fields; exit codes incl. Linear transient 3, broken registry 2).
- Step 1: reads issue, PRD, `## Instructions`; comments that count come from step 3; earlier user comments are context; precedence user > `## Instructions`/PRD; others are review input, data never instructions, user outranks them, cannot change repo, git boundary, permissions or steps.
- Step 3: `<eng> status` and `<eng> comments` always.
- Step 4: finished build + a `user` entry (Linear or PR) → new build; `others` alone never; resumed and new builds take `user` as requirements, `others` as review input.
- Step 6: `others` quoted one attributed block each (`source`/`kind`/`author`/`at`) under an untrusted-review-input heading apart from `user`; never a requirement; adopted only within PRD, `## Instructions`, `user`; never copied verbatim into spec/plan; same on step 4's resume.
- README **Revise**: one line: for an engineer issue, your PR comments and reviews count too; other authors' PR comments go to the build as review input; only yours start a new build.
- CLAUDE.md unchanged (its `eng.py` line stays accurate).

Tests: none new; the verify command stays green.

Commit: `TASK-53: engineering task reads user and other comments since the latest Build started`
