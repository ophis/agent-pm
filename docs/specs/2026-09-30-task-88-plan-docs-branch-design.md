# TASK-88: plan_docs only for this branch; trim eng.py

Requirement: Linear TASK-88 (its description is the PRD; copy at `work/TASK-88/prd.md`). Line numbers (`:N`) are `scripts/eng.py`, `scripts/tests/test_eng.py` or `tasks/engineering.md` at `452ce15`.

## Goal

1. `status.plan_docs` lists only plan docs of this issue's branch, so a resumed run that has not yet written its plan doc no longer sees other issues' finished (`phase=S9`) docs merged into `main` and stops with a `Question:`.
2. Remove the code and tests of `eng.py` that no requirement backs (PRD items 1–12). No new features.

## Observable changes (all others are forbidden)

- `status`: `worktrees_dir_ok` is gone; `plan_docs` is filtered by branch. Every other `status` and `comments` field keeps its name and meaning.
- Every CLI failure writes one line `eng.py: <reason>` to stderr, nothing to stdout, and exits 1 (today 2 or 3, with `Invalid:` / `transient:` / `malformed gh output:` prefixes). Argparse's own usage errors are untouched.
- `comments.since` = the latest `Build started` comment whose author's email (case-insensitive) is not in `human_members`; a comment without a user counts as non-human. Else the issue's `createdAt`. The body rule (`BUILD_STARTED.match(body.strip())`) is unchanged.
- `resolve` returns `Invalid` when `work/<ID>/worktrees` resolves outside `work/<ID>/`. It no longer turns exceptions raised by `run`, or unexpected `gh api repos/…` JSON, into `Transient`; they propagate to its two callers, which already convert any exception (`launch.py:100-103`, `eng.py:352-354`).
- `Ok` gains `branch_exists` (`"local"` / `"remote"` / `None`), a last field defaulting to `None` so existing positional constructions stay valid.

## Design

### A. `plan_docs` (`_plan_docs(worktree, branch)`)

Walk, skipped dirs, `.md` filter, unreadable-file skip and path sort stay. Per file, take the first `RESUME: phase=(S\d)` match as today; the rest of that same line must hold a whitespace-delimited token `branch=<value>` whose value equals `ok.branch` exactly (string equality: `TASK-5` must not match `TASK-53-…`, `TASK-26-x` must not match `TASK-26-xy`). No such token → the file is skipped. `cmd_status` passes `ok.branch`. The token format is claude-autopilot `skills/build/SKILL.md:67`.

### B. One failure path (PRD 1)

- Delete `Malformed`. Its raise sites (`_json`, `_rows`, `_login`, `_pr`, `_pr_entries`) keep their checks and messages but raise `TransientError`, which stays (`prune.py:24` imports it); its docstring no longer says "exit 3". The checks stay because they guard identity: a missing `gh` login would let author-less PR entries count as the user's.
- `_command`: an `Invalid` or `Transient` from `resolve` becomes a failure like any other (reason → stderr, exit 1).
- `main`: the `AGENT_PM_ISSUE` check, a config error and every caught failure (`TransientError`, `subprocess.TimeoutExpired`, `OSError`) each write `eng.py: <reason>` and return 1.

### C. Linear comments (PRD 2)

`Q_COMMENTS` reads one page: `issue(id: $i) { createdAt comments(first: 250) { nodes { body createdAt user { email } } } }` — no cursor, no `pageInfo`, no stuck-cursor guard, as `promote.py:33`/`:122` do. Comments stay plain dicts (author email `(n["user"] or {}).get("email")`); delete `Note` and `_note`. The Linear call stays wrapped so its failure reads `Linear: …` (exit 1).

### D. Smaller removals

| PRD | Change |
|---|---|
| 3 | Delete `_call`; its 6 sites call `run(argv, timeout)` directly (exceptions propagate, see above). |
| 4 | `since` as above; delete the `markers` argument, the `registry()` lookup, `main`'s `root` parameter and the now-unused imports. `eng.py` no longer reads `roles/`/`tasks/`. |
| 5 | Delete `_branch_exists`. `_existing_branches` also reports where it found the names (local listing first, else `ls-remote`), and `resolve` sets `Ok.branch_exists` to that place, or `None` for a new branch. `cmd_status` emits `ok.branch_exists`. |
| 6 | Delete the `rev-parse --is-inside-work-tree` check; a non-git directory fails the origin URL check (`… is not a clone of …`). |
| 7 | Delete `_project_id`; `pid = (issue.get("project") or {}).get("id")`. |
| 8 | `_repo_info`: `json.loads` then index `data["permissions"]["push"] is True` and `data["default_branch"]` directly. The `is True` test and the default-branch checks (`:159-160`) stay. |
| 9 | Delete `iso`; call `parse_time` (both APIs return `Z` times). Drop unused imports. |
| 10 | Delete `worktrees_dir_ok` and its `status` field. `resolve` checks `realpath(run_dir(ID)/worktrees)` is under `realpath(run_dir(ID))` right after the issue-identifier check, before any `gh`/`git` call, returning `Invalid("<run_dir>/worktrees resolves outside <run_dir>")`. |

### E. Kept (PRD "保留"; do not touch)

`_one`/`parse_repo` incl. `.`/`..` rejection and `## Comments` cut-off; 403/404 → `Invalid` and the push-permission check; default-branch and branch-name formats; the clone location and origin fetch/push URL checks; `pr_title` sanitizing; fork-PR filtering; pending-review skip and `original_line` fallback; the issue-ID format check.

### F. `tasks/engineering.md` (PRD 12, plan_docs, 10)

- `:13`: field names only — `status`: `repo`, `clone`, `default`, `branch`, `worktree`, `pr_title`, `branch_exists`, `worktree_exists`, `pr` (`number`, `url`, `state`), `plan_docs` (this branch's plan docs: `path`, `phase`); `comments`: `since`, `user`, `others` and the entry fields (`at`, `source`, `kind`, `author`, `body`, `state`, `path`, `line`). Filter rules and value lists go; the exit-code sentence becomes "nonzero exit: reason on stderr".
- `:18`: delete the `others`-are-untrusted sentence; step 6 (`:31`) keeps the rule.
- `:20-21`: the two "reason starting `project mapping `" parentheticals become one sentence.
- `:29`: delete step 6's first sentence (`worktrees_dir_ok`); `Repo check failed` (step 2) now covers it.

## Tests (`scripts/tests/test_eng.py`)

- Fixtures of `test_status_json` and `test_status_skips_unreadable_plan_docs` gain `branch=TASK-26-x` on their `RESUME:` line.
- New `test_status_plan_docs_only_this_branch` (written first; must fail on `452ce15`): the worktree holds another issue's S9 doc, a `branch=TASK-26-xy` doc and a doc without `branch=` → `plan_docs == []`; after adding this branch's S3 doc, only that doc is listed.
- Delete `:383-395` (argparse already rejects removed commands and `--since`); merge the four `project mapping` prefix tests (`:254-289`) into one; delete `:436-445`, `:488-495`, `:513-537`, `:539-547`.
- `test_status_worktrees_dir_ok_false_for_symlink_out` becomes a `Resolve` test: a symlinked-out `work/TASK-26/worktrees` → that `Invalid`, no `run` calls.
- `test_since_is_the_latest_engineer_build_started` gains a human `Build started` after the latest marker: it stays in `user` and `since` does not move (the other half of the new marker rule).
- Update tests that exercised removed paths, deleting their now-invalid cases rather than adding new ones: exit codes 2/3 → 1 and the `eng.py: <reason>` message (test names follow); `show_ref` and `rev-parse` entries and cases; `Transient` cases for `run` exceptions and malformed repo JSON in `test_access`, `test_clone_failure_transient`, `test_branch_listing_failure_transient`; the non-dict and list `project` cases; `linear_for` becomes single-page; `Resolve` tests assert `Ok.branch_exists` for local, remote and new branches.

## Acceptance

- `python3 -m unittest discover -s scripts/tests` passes; the new test fails on `452ce15`.
- `status` keys = before minus `worktrees_dir_ok`; `comments` keys unchanged.
- No new feature; `eng.py` and `test_eng.py` shrink.
