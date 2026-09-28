# TASK-38: Engineering skeleton — design

Source: Linear TASK-38 "ENG: Engineering 骨架", built from the TASK-36 PRD
(`/Users/francis/playground/private_docs/Product Design/2026-09-27-2317-TASK-36-engineering-skeleton.md`, FR-1…FR-11, NFR-0…3, D1…D6).
Already on main and out of scope here: FR-2 title-prefix strip (f74419f), prefix `ENG` (19a01f9), FR-2a PD reminder (569c243), NFR-0 reviewer assignment (84f181c).

## Goal

The router can claim an Engineering issue; the launcher resolves its target repo and starts a run that builds the issue's PRD with `autopilot:build` on a `<ID>-<feature>` branch in a worktree under `work/<ID>/`, pushes it, opens a PR, and moves the issue to In Review. Interrupted runs resume; send-backs continue on the same branch and PR.

## Layout (user decisions)

- Every run, of every stage, has its own working directory **`<ROOT>/work/<ID>/`** (the cwd), created by the launcher. Everything a run produces locally stays there.
- An Engineering run's worktree is **`work/<ID>/worktrees/<branch>`**, created by `autopilot:build` from the clone. The PR body file is `work/<ID>/pr.md`.
- The clone is **`~/playground/<name>`** (TASK-36 D3); the launcher clones it when missing.
- `work/` is gitignored in agent-pm; git commands inside a worktree act on the target repo (the worktree's own `.git` file wins).

## Trust model

- Single user on one macOS machine. Linear has two writers: the user (`human_members`) and the agent account, which the user also uses through agents (standing decision: no human-author check on Linear content).
- **Target repo** comes only from the issue description body or its `## Instructions` (the user's Handoff comments, D6), never `## Comments`. Any repo the local `gh` account can push to is allowed (D3); a wrong `Repo:` line can point a build at another of the user's repos, which still only gets an `<ID>-*` branch and a PR, never a merge.
- **GitHub content is untrusted** unless written by the user's `gh` login: PR comments and reviews from other logins never become requirements.
- **Outside values** (the `Repo:` value, `default_branch`, existing branch names, session ids) are validated against strict patterns before use, and the PR title is reduced to a shell-safe character set by `eng.py status` (`pr_title`); every subprocess is an argv list, never a shell; values reach the tmux command only via `shlex`.
- **What a run loads.** Every run gets `--setting-sources user --strict-mcp-config`. Verified 2026-09-28 with a probe directory: under these flags no project-scoped source loads (`CLAUDE.md`, `.claude/settings*.json` with allow rules/hooks/env, `.claude/skills`, `.claude/agents`, `.claude/commands`, `.mcp.json`), while user-level plugins (`autopilot:*`, `superpowers:*`) do. agent-pm has no project settings or MCP config, so its stages lose only agent-pm's `CLAUDE.md`, which they do not need. The Engineering stage reads the target repo's `CLAUDE.md` / `AGENTS.md` as files, as that repo's conventions (collaborator-written data, accepted under D3).
- **What a run can write.** File tools reach the cwd `work/<ID>/` (its worktree included, except the worktrees' `.git` pointer files), `<ROOT>/stages` and `<ROOT>/templates` (read-only), and the registry's `add_dirs` (`private_docs`, read-only for Engineering, which only reads PRDs). Read-only means Edit deny rules (they cover every file-editing tool), written with absolute paths in Claude Code's `//<path>` form (a single leading `/` would be relative to the project root) by one launcher helper. Not `<ROOT>` as a whole: no other issue's directory, no agent-pm code, `.git` or `.claude`. Bash commands still go through the auto-mode classifier.
- **No allow rules by default.** A wildcard rule like `Bash(git push origin <ID>-*)` would also allow `:main`, `--force`, `--delete`; none is used. Remediation (FR-4a, PRD R1): if the supervised first run shows the classifier stopping a push, add an **exact** rule template to the project's `allowed_tools` in `pipeline.toml`, e.g. `Bash(git -c core.hooksPath=/dev/null -C {worktree} push -u git@github.com:{owner}/{name}.git {branch})` (no hooks, explicit URL, so a retargeted gitdir config cannot run commands), which the launcher fills from validated values; the stage's push command is then that exact string. Config load rejects templates with `*`, unbalanced braces, unknown placeholders (allowed: `{worktree}` `{branch}` `{owner}` `{name}` `{default}` `{clone}`), `<ROOT>` (also as `~/…`, `$HOME/…`, `${HOME}/…`), an interpreter on a script path (incl. versioned, e.g. `python3.12`) or a build tool/runner (`make`, `npm`, `npx`, `pnpm`, `yarn`, `bun`, `cargo`, `go`, `pytest`, `uv`), and `allowed_tools` on a project without `repo_from_issue`.
- **Accepted, not enforced here:** protection of the default branch rests on the classifier and the stage text (the user may add GitHub branch protection); spec and plan docs committed to a public target repo are public (D3); a run may set `AGENT_PM_ISSUE` to another issue for `eng.py`, bounded to that issue's resolved repo.

## Components

### 1. `pipeline.toml` — Engineering becomes runnable

```toml
[projects."ddbff8bf-…"]  # 3-Engineering
prefix = "ENG"
instructions = "stages/engineering.md"
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]
repo_from_issue = true
```

### 2. `scripts/pipeline.py` — shared helpers

- `PROJECTS = ~/.claude/projects`; `escape(path)`: Claude Code's folder name for a cwd (every non-alphanumeric character → `-`); `run_dir(issue) = <ROOT>/work/<issue>`.
- `transcript(issue, sid)`: `<PROJECTS>/<escape(run_dir(issue))>/<sid>.jsonl`, for a UUID `sid` only (else None). Replaces the fixed `TRANSCRIPTS` constant and TASK-20 FR-A5 (no cwd needs recording: the cwd is a function of the issue).
- `load_config` validates `allowed_tools` as in the Trust model.

### 3. `scripts/eng.py` — repo resolution (library)

`resolve(issue_id, gql, run) -> Ok | Invalid | Transient`, used by the launcher and the CLI:
- `Ok(issue, title, owner, name, clone, default, branch, worktree)`; `Invalid(reason)`: user-fixable, the issue is bounced; `Transient(reason)`: infrastructure, retried later.

Steps:
1. **Read** the issue's `identifier`, `title`, `description` (`gql`). Any error → `Transient`; `issue: null` or an `identifier` other than the requested id (case-sensitive) → `Invalid`.
2. **Parse** `Repo:` above the first line exactly `## Comments`; a line counts when it matches `^\s*repo:\s*(.+)$` case-insensitively. Accepted values: `owner/name`; `https://github.com/owner/name` (optional `.git`, trailing `/`); `git@github.com:owner/name(.git)`; any of these inside a Linear markdown link `[text](<url>)` / `[text](url)`. Owner `[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?`; name `[A-Za-z0-9._-]{1,100}`, not starting with `-`, not `.`/`..`. None, several distinct values (case-insensitive) or a bad form → `Invalid`.
3. **Access** `gh api repos/<owner>/<name>`: HTTP 404/403 or `permissions.push != true` → `Invalid`; other failures/timeouts and malformed JSON → `Transient`. `default_branch` must match `[A-Za-z0-9._/-]+`, no leading `-`, no `..` → else `Invalid`.
4. **Clone** `~/playground/<name>`: a symlink or a path resolving outside `~/playground` → `Invalid`. Exists → a git work tree whose `origin` fetch and push URLs normalize (ssh/https, optional `.git`, case-insensitive) to `github.com/<owner>/<name>`, else `Invalid`. Absent → `gh repo clone <owner>/<name> <path>`; failure/timeout → `Transient`.
5. **Branch**: the existing `<ID>-*` branch (local, else `origin` via `git ls-remote --heads`), else `<ID>-<slug>` (slug: title without a leading `PREFIX: `, lower-case ASCII letters/digits joined by `-`, ≤ 40 chars, empty → `build`). Several existing, or one not matching `<ID>-[a-z0-9-]{1,40}` → `Invalid`; a failing `git branch --list` / `ls-remote` → `Transient`. **Worktree**: `run_dir(ID)/worktrees/<branch>`.

Timeouts: git/gh 60 s, clone 600 s; `TimeoutExpired`, `FileNotFoundError`, `OSError` → `Transient`.
Known limitation: each call re-reads the issue, so editing `Repo:` or the title mid-build can change the result (e.g. another branch name before the branch exists); `eng.py status` is the source of truth for the run.

### 4. `scripts/eng.py` — CLI for the Engineering run

`python3 <ROOT>/scripts/eng.py <command>`; the issue comes from env `AGENT_PM_ISSUE` (set by the launcher, must match `[A-Z][A-Z0-9]*-\d+`). Each command re-runs `resolve` and refuses (exit 2, reason on stderr) on `Invalid`; `Transient`, a failing `gh`/`git` call or a subprocess error exits 3 (`eng.py: transient: …`), malformed `gh` output (bad JSON or the wrong JSON type) exits 2; never a traceback.

| Command | Does |
| -- | -- |
| `status` | JSON: repo, clone, default, branch, worktree, `pr_title` (`<ID>: <title without prefix>` with every character outside Unicode letters and digits, spaces and `.,:()-_/` replaced by a space, runs of spaces collapsed), `worktrees_dir_ok` (`work/<ID>/worktrees` does not resolve outside `work/<ID>/`), `branch_exists` (local/remote/none), `worktree_exists`, the branch's PR (number, url, state) if any, counting only non-fork PRs by the `gh` login, autopilot plan docs in the worktree (readable files with a `RESUME: phase=` line) and their phase. |
| `comments --since ISO` | JSON of PR comments and reviews (all pages) after `ISO` (compared as times) written by the `gh` login (`gh api user`); others are dropped and counted. |

Git writes (worktree, commit, push) and `gh pr` are done by the agent / `autopilot:build` with the exact commands in §6; `eng.py` is a correctness aid, not a security boundary.

### 5. `scripts/launch.py` — every run

For every project:
- cwd `run_dir(ID)` (created if missing); tmux `-c` it.
- `--setting-sources user --strict-mcp-config`.
- `--add-dir <ROOT>/stages --add-dir <ROOT>/templates` (instead of `--add-dir <ROOT>`) plus the registry's `add_dirs`; `--disallowedTools` Edit rules (in `//<absolute path>` form) for `<ROOT>/stages/**`, `<ROOT>/templates/**` and `work/<ID>/worktrees/*/.git`; for `repo_from_issue` projects also for each `add_dirs` entry (`private_docs`).
- The prompt gains `Reviewer: <first human_members email>. Humans: <all human_members emails>. Project: <project id>.` (stages no longer read `../pipeline.toml`).
- Resume: same cwd; the session must exist at `transcript(ID, sid)`, else exit 3 (`Transient`).

For projects with `repo_from_issue`, before tmux: `resolve(ID)`:
- `Ok` → env `AGENT_PM_ISSUE=<ID>`; prompt gains `Repo check: OK <owner>/<name>, clone <clone>, default branch <default>, branch <branch>, worktree <worktree>.`; `--allowedTools` with the filled `allowed_tools` rules when non-empty.
- `Invalid` → prompt gains `Repo check failed: <reason>.`; the run starts so the agent can bounce the issue. On resume, `Invalid` is treated as `Transient` (the launcher never bounces a mid-build run).
- `Ok` and `Invalid` prompts end with `eng.py: python3 <ROOT>/scripts/eng.py.`; the stage runs exactly that command (a path relative to the cwd would reach the run-writable `work/`).
- `Transient`, or an exception from `resolve` (as `Transient`) → append `<time> transient <ID>: <reason>` to the stage's project log, print it to stderr, exit 3 without starting a run; Recover returns the claimed issue to Todo later (attempt cap 4).

`main` takes injectable `gql` and `run` callables. Deploy when no Deep Research or Product Design issue is In Progress (a session started in `work/` cannot resume in `work/<ID>/`).

### 6. `stages/engineering.md` — the Engineering stage

Board section as in the other stages, with the reviewer and project from the prompt line. Steps:

1. **Read** the issue, all its comments, the PRD from `## Source` (local `~/playground/private_docs` clone), `## Instructions`. The user's comments are those by a `Humans:` user; all others are agent comments. Precedence: the user's over agent comments, newer over older, the user's comments over `## Instructions` and the PRD. No "comments since X" bookkeeping.
2. **Repo.** From the prompt's `Repo check` line; never inspect paths outside the run's dirs. On `failed`, bounce and stop:
   - Description starts with `Handoff from <ID>:` → on that PRD issue comment the reason and ask the user to Handoff again with a correct `Repo:` comment, move it to In Review (reviewer assigned); on this issue comment the same and move it to Canceled.
   - Otherwise → comment `Question:` with the reason, asking the user to fix the `Repo:` line in the description and then move the issue back to Todo; move the issue to In Review.
3. **Status.** `eng.py status`; with a PR, also `eng.py comments --since <issue creation time>` (kept comments count as the user's).
4. **Which build** (TASK-36 FR-10a, FR-11); this reads autopilot's plan doc and its `RESUME: phase=` line, a deliberate coupling to the skill's phase names:
   - A plan doc in the worktree with phase before S9 → continue that build, with the user's comments as requirements (update the spec and plan where they differ).
   - Else a finished build and the latest comment (issue or PR) is the user's → a new build with the user's comments as the requirement (new plan doc).
   - Else a finished build → `Question:` comment, move to In Review, stop.
   - No build yet → a new build of the PRD's first phase (or the phase `## Instructions` names).
5. **Start.** On a new (not resumed) run, comment `Build started`.
6. **Build.** If `eng.py status` shows `worktrees_dir_ok: false`, fail (step 8) without building. Run `autopilot:build` with a requirement stating (this relies on the skill following requirement text over its own worktree setup — a second deliberate coupling to its internals, checked in the supervised run):
   - the PRD path, `## Instructions` and the user's comments, with step 1's precedence, and "read the target repo's `CLAUDE.md` / `AGENTS.md` in the worktree as its conventions";
   - "use the worktree `<worktree>` on branch `<branch>`: if it does not exist, `git -C <clone> fetch origin`, then `git -C <clone> worktree add -b <branch> <worktree> origin/<default>` for a new branch, `git -C <clone> worktree add <worktree> <branch>` for an existing local one, or `git -C <clone> worktree add --track -b <branch> <worktree> origin/<branch>` for a remote-only one; then work only there, with absolute paths; create no other worktree or branch";
   - "put the spec and plan doc where the target repo keeps design docs (e.g. an existing `docs/specs/`), else in `autopilot_docs/` at the repo root; commit them on the branch";
   - "skip S8: keep the commits"; "after each implementation task and each review round run exactly `git -C <worktree> push -u origin <branch>`"; "never force-push, never merge, never touch `<default>`".
7. **PR** (build converged): `git -C <worktree> push -u origin <branch>`; comment `Build docs:` with the GitHub links of the build's spec and plan doc on the branch (`https://github.com/<owner>/<name>/blob/<branch>/<path>`, paths from `eng.py status`); write `work/<ID>/pr.md` (what changed, PRD and spec links, how to verify, residual non-blocking items); if `eng.py status` shows no PR, `gh pr create --repo <owner>/<name> --head <branch> --base <default> --title '<pr_title>' --body-file work/<ID>/pr.md`, else `gh pr edit <number> --repo <owner>/<name> --body-file work/<ID>/pr.md`; attach the PR URL to the issue; comment `Build ready:` + 3–5 lines incl. how to verify; assign the reviewer. The GitHub integration moves the issue to In Review when the PR opens (Linear rule "On PR open → In Review", set 2026-09-28), so the agent does not move it: it reads the state and moves it to In Review only if it is not there (e.g. the PR already existed, or the integration lagged). Fallbacks: push denied → `Build failed:` "push not permitted" and move to In Review; PR creation denied → attach `https://github.com/<owner>/<name>/compare/<default>...<branch>?expand=1` and say in `Build ready:` that the user must open the PR.
8. **Failure** (build stopped or capped, or an action it needs was denied by the permission classifier): push the branch; comment `Build docs:` with the spec and plan links as in step 7 (whatever exists); comment `Build failed:` with the failing tests, open blockers or denied action, and the branch URL; move to In Review.

Resume rule: a prompt starting "Resumed run" re-reads this file, uses `eng.py status`, never repeats `Build started`, never re-creates a branch, worktree or PR, never moves the issue to Todo.

### 7. `scripts/router.py`

Liveness, the resume candidate and Recover's "has a transcript" check use `transcript(issue, sid)`; `is_live` checks that file and its `<folder>/<sid>/` subagent files. The `start` line's `transcript=` is that path. `tdir` becomes the projects root (`PROJECTS`).

### 8. Deep Research and Product Design stages

Their Board bullet takes the reviewer and project id from the prompt's `Reviewer: … Project: …` line instead of `../pipeline.toml`. Nothing else changes (Product Design still reads `../templates/prd.md`, now via `--add-dir <ROOT>/templates`).

### 9. Docs

README.md and CLAUDE.md: the `work/<ID>/` layout; flags every run gets; Engineering runnable; `repo_from_issue`, `allowed_tools`; the launcher's repo step and its three outcomes; `eng.py`; the `Repo:` line in the Handoff comment; autopilot docs committed in the target repo (its docs place, else `autopilot_docs/`).

Out of scope, follow-up: removing `work/<ID>/` and its worktrees (`git worktree remove`) once an issue is Done or Canceled.

## Testing

- `test_eng.py` (fake `gql`/`run`): parsing (each form, Linear link, key case, CRLF, several values, `## Comments` ignored incl. inside Instructions, bad owner/name incl. leading `-`); access (404/403/no push → Invalid; 5xx/timeout/missing gh → Transient; unsafe `default_branch` → Invalid); clone (symlink, plain dir, origin fetch/push mismatch incl. ssh/https/case, absent → clone, clone failure → Transient); branch (existing local/remote, several, unsafe name, slug rules incl. all-Chinese title); worktree path is `work/<ID>/worktrees/<branch>`; a branch that exists only locally (crash after `worktree add`, before the first push) with its worktree present resolves to the same branch and worktree; issue null / other identifier → Invalid; malformed repo JSON, failing branch listing → Transient; CLI: missing/invalid `AGENT_PM_ISSUE` refuses, no `setup`/`push`/`pr` commands, `status` JSON incl. plan docs (unreadable files skipped), `pr_title` sanitizing (e.g. `$(`, backticks, quotes, newlines; CJK kept), `worktrees_dir_ok` false for a symlink out, fork PRs and other authors' PRs ignored, `comments` drops other logins, reads every page, compares times; `Transient`, failing `gh`/`git` or a subprocess error → exit 3; malformed `gh` output → exit 2.
- `test_pipeline.py`: `escape`, `run_dir`, `transcript` (UUID only); `allowed_tools` validation (accepts an exact template; rejects `*`, unbalanced braces, unknown placeholder, `<ROOT>` incl. `~/…`/`$HOME/…`, interpreter+script incl. `python3.12`, build tools/runners, missing `repo_from_issue`); the real `pipeline.toml` loads with Engineering runnable.
- `test_router.py`: transcripts under `PROJECTS/<escape(work/<ID>)>/`; liveness, resume candidate, Recover's no-transcript path.
- `test_launch.py`: every project: cwd `work/<ID>/` created, `--setting-sources user --strict-mcp-config`, `--add-dir` stages/templates (not `<ROOT>`), the deny rules in `//` absolute form (incl. the `.git` pointer rule; Engineering also `private_docs`), the `Reviewer/Project` prompt line; resume with no transcript at `transcript(ID, sid)` → exit 3; Engineering `Ok` (env, prompt lines incl. `eng.py:`, no `--allowedTools` when empty, filled exact rules when set), `Invalid` (reason and `eng.py:` lines; on resume → exit 3), `Transient` and a `resolve` exception (exit 3, no tmux, reason in the project log).
- Full suite `python3 -m unittest discover -s scripts/tests` passes.
- Manual, supervised first Engineering run (TASK-26 once handed off): `gh` auth under launchd → tmux, no permission prompts (incl. autopilot creating the worktree and git writing the clone's `.git`), the probe repeated in a real worktree, an Edit on the stage file and on a worktree's `.git` is denied, autopilot used `<worktree>` and created no other worktree or branch, the `Build docs:` comment links resolve, resume.
