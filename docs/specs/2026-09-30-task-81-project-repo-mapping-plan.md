# TASK-81: project → repo mapping — plan

RESUME: phase=S9 worktree=/Users/francis/playground/agent-pm/work/TASK-81/worktrees/TASK-81-build branch=TASK-81-build base_ref=0651ba0e2469f8607ff782b9a0001a9c10da8736 review_round=0 spec_file=docs/specs/2026-09-30-task-81-project-repo-mapping-design.md

## Progress

- S1: reused the existing worktree (branch TASK-81-build at origin/main 0651ba0).
- S2: spec written (architectural: config schema + `resolve` interface).
- decision(mapping plumbing): chose a `repos` dict argument to `resolve` + `Ok.mapped` flag over resolve loading config itself - PRD FR-3 says the caller passes it; the flag is the launcher's only way to annotate; dissent: none.
- decision(prefix wording): chose `project mapping ` + today's `<owner>/<name>: …` reason over a second copy of the repo - satisfies FR-6's prefix without repetition; dissent: none.
- decision(CLI config error): chose exit 2 (`eng.py: <msg>`) over an uncaught SystemExit (exit 1) - keeps the documented exit codes; dissent: none.
- S3 panel: core=[architecture,spec-fitness] +optional=[security] (mapping values feed clone path and gh calls) transport=Workflow
- S3 r0: architecture=PASS spec-fitness=PASS security=PASS -> converged; folded NB: `pipeline.repo_slug` shared rule, prefix constant, malformed `project` = no project, exact-mapping test (MISC absent), resolve re-checks mapping values via `repo_slug`. Kept `repos=None` default (NB: required kw) - one default keeps direct test calls simple.
- S4: plan written (4 tasks: config, resolve+CLI, launcher, docs).
- S5: tasks 1-4 done via subagent-driven-development, each task review Approved (b63097a, 0ecffd7, 88b5594, 7c3be8a); deferred minors: loop-only-last-call assertions in test_eng, eng.main catches only SystemExit from load_config, engineering.md step 2 parenthetical wording.
- S6: unittest 302 OK, no warnings; read-only live check with the real pipeline.toml: Agent PM + no Repo: line -> Ok ophis/agent-pm mapped=True; unmapped / no project -> today's Invalid; Repo: line wins (mapped=False); gh access to ophis/claude-autopilot OK (push, main); 404 mapping -> `project mapping ophis/…: not found or no access (HTTP 404)`.
- S7 panel: core=[correctness,requirement-fidelity,doc] +optional=[test,code-quality] (performance: one config load per CLI call, marginal; architecture: structure reviewed at S3 and follows spec) transport=Workflow
- S7 r0: correctness=PASS requirement-fidelity=PASS doc=PASS test=PASS code-quality=PASS -> converged
- S8: skipped (keep the commits, per the requirement)
- Residual non-blocking: `eng.main` catches only `SystemExit` from `load_config` (a missing or unparseable `pipeline.toml` gives a traceback, exit 1, as in the other scripts); `project_repos.<key>` in the error message is not quoted; `tasks/product-design.md` still asks for `Repo:` in the Handoff comment (unchanged per the PRD's non-goal, still true for unmapped projects); `NO_LINE = parse_repo("")` sentinel and the `mapped`/`tag` pair in `resolve` could be tighter; some `test_eng` loops check `run_.calls == []` only for the last call; most `Cli` tests read the real `pipeline.toml`.

## Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Spec:** `docs/specs/2026-09-30-task-81-project-repo-mapping-design.md`

## Global Constraints

- Worktree `/Users/francis/playground/agent-pm/work/TASK-81/worktrees/TASK-81-build`, branch `TASK-81-build`; absolute paths / `git -C <worktree>` only; before any write assert `git -C <worktree> branch --show-current` is `TASK-81-build`. Never touch `main`, never force-push, never merge.
- After each task's commit run exactly `git -C /Users/francis/playground/agent-pm/work/TASK-81/worktrees/TASK-81-build push -u origin TASK-81-build`.
- Follow the repo's `CLAUDE.md`: terse code in the file's existing style, comments only for a gotcha, docstrings one line; commit messages `TASK-81: <what>`.
- Verify: `python3 -m unittest discover -s scripts/tests` from the worktree (no network, Keychain or Claude). Tests never touch network, Keychain or Claude.
- Unmapped behaviour and prompt strings stay byte-identical; existing test expectations stay, except `test_launch`'s `resolve` call assertion (gains `repos=`).
- Exact values: prefix `project mapping `; annotation ` (from project mapping)` right after `<owner>/<name>` in `Repo check: OK`; shipped entries `"121166b1-191a-4461-bec4-42f1c2dc0ddd" = "ophis/agent-pm"` (Agent PM), `"ae72ede7-67a6-469d-a959-8ea51ab71fb8" = "ophis/claude-autopilot"` (Autopilot); no other entry.
- Scope is the spec only (prototype): no warning on a differing `Repo:` line, no PM prompt change, no Linear/GitHub lookups at load time.

---

### Task 1: `[project_repos]` config

**Files:** `scripts/pipeline.py`, `scripts/eng.py` (imports only + `_one`'s bare form), `pipeline.toml`, `scripts/tests/test_pipeline.py`.

**Produces:** `pipeline.OWNER`, `pipeline.NAME` (moved verbatim from `eng.py`), `pipeline.repo_slug(value) -> (owner, name) | None` (bare `owner/name`, fullmatch, name not `.`/`..`; non-string → `None`); `load_config(path)` returns `cfg["project_repos"]` always present (`{}` when absent); `TOP_KEYS` includes `project_repos`. `eng.OWNER`/`eng.NAME` remain importable names in `eng` (imported from pipeline).

**Tests first:** absent table → `{}`; the real `pipeline.toml` → exactly the two shipped entries; two project ids → same repo accepted; each rejected with `SystemExit` whose message starts `pipeline.toml: project_repos.<key> ` and contains the value's repr: non-UUID key, uppercase-hex key, `https://github.com/ophis/x` value, `ophis/.`, `ophis/..`, `""`, `42`, `ophis`; `project_repos = "x"` (not a table) rejected with message starting `pipeline.toml: project_repos`; `runnable` accepts a config with `[project_repos]`; `repo_slug` unit cases (`ophis/agent-pm`, `ophis/.github` ok; `-a/b`, `a/-b`, `a/..`, `a b/c`, None → None). Existing `ParseRepo` tests in `test_eng.py` keep passing unchanged.

**Commit:** `TASK-81: [project_repos] in pipeline.toml, validated by load_config`

### Task 2: resolve by `Repo:` line, else project mapping; eng CLI loads the mapping

**Files:** `scripts/eng.py`, `scripts/tests/test_eng.py`.

**Consumes:** `pipeline.repo_slug`, `pipeline.load_config`, `pipeline.CONFIG`.

**Produces:** `eng.MAPPED = "project mapping "`; `Q_ISSUE` with `project { id }`; `Ok(..., mapped: bool = False)` as last field; `resolve(issue_id, gql, run, playground=PLAYGROUND, repos=None)`; `main(argv, env=os.environ, gql=None, run=sh_run, out=sys.stdout, err=sys.stderr, config=CONFIG)` loading `config` after the `AGENT_PM_ISSUE` check, `SystemExit` → `eng.py: <message>` on stderr, exit 2; `_command` passes `repos=cfg["project_repos"]`.

**Tests first** (gql fakes gain an optional `project`): mapped + no line → `Ok` owner/name from the mapping, `mapped=True`, `gh api repos/<mapped>` called; mapped + different line → the line's repo, `mapped=False`; mapped + unreadable line and mapped + two different lines → `Invalid` with today's reasons; unmapped project + no line and `project: None` + no line → exactly `Invalid("no Repo: line in the description or its ## Instructions")`; `project` a string / `id` a list → treated as no project, no exception; mapped repo HTTP 404, 403, `push: false`, unsafe default → reason `== MAPPED + <today's reason>` (starts `project mapping ophis/<name>: `); same failures from a `Repo:` line → today's reason (no prefix); mapped repo with a clone whose origin differs → today's reason; mapping value failing `repo_slug` → `Invalid` starting `MAPPED`. CLI: `main(..., config=<tmp toml with a mapping>)` passes that dict as `repos=` to `resolve`; a broken config (bad entry) → rc 2 and stderr starts `eng.py: pipeline.toml: project_repos.`.

**Commit:** `TASK-81: eng.resolve falls back to the project mapping when there is no Repo: line`

### Task 3: launcher passes the mapping and marks its source

**Files:** `scripts/launch.py`, `scripts/tests/test_launch.py`.

**Consumes:** `eng.resolve(..., repos=)`, `Ok.mapped`, `cfg["project_repos"]`.

**Produces:** `repo_step(a, task, gql, run, repos)` calling `eng.resolve(a.issue, gql, run, repos=repos)`; `main` passes `cfg["project_repos"]`.

**Tests first:** a mapped `Ok` → tail ` Repo check: OK ophis/demo (from project mapping), clone /u/playground/demo, default branch main, branch TASK-1-demo, worktree <wt>. eng.py: python3 <ENG_PY>.`; unmapped `Ok` tail unchanged (existing test); the `resolve` call assertion becomes `("TASK-1", self.gql, self.run, repos=<the fixture config's project_repos>)`; a fixture config with `[project_repos]` → that dict reaches `resolve`.

**Commit:** `TASK-81: the launcher resolves with the project mapping and notes it in Repo check`

### Task 4: docs

**Files:** `README.md`, `CLAUDE.md`, `tasks/engineering.md`.

**Content (spec G4, G5):** README "New work" (a direct engineer issue needs `Repo:` only when its project has no `[project_repos]` entry), "Approve" (a PRD's Handoff comment needs `Repo:` only when the project is unmapped; a `Repo:` line always wins), "Configuration" (`[project_repos]`: project id → `<owner>/<name>`). CLAUDE.md `eng.py` bullet (repo from the `Repo:` line, else the project's `[project_repos]` entry). `tasks/engineering.md`: Board "a bad `Repo:` line or project mapping"; Inputs — `Repo check: OK` may carry `(from project mapping)` after the repo, and `## Instructions` holds the `Repo:` line (optional in a mapped project); step 2 — a reason starting `project mapping `: Handoff-created → on the PRD issue ask to fix the project's `pipeline.toml` `[project_repos]` entry (or add a `Repo:` line) and Handoff again, PRD to In Review, this issue Canceled; direct → `Question:` asking to fix the mapping (or add a `Repo:` line) and move back to Todo, In Review. Other reasons keep today's bullets. Keep edits minimal, one rule one place; `tasks/product-design.md` unchanged.

**Tests:** full suite still passes (no prompt string asserted from these files changes).

**Commit:** `TASK-81: docs: Repo: is optional in a mapped project`
