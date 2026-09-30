# Engineering (build)

Turns one Engineering issue into a pull request on its target repo: `autopilot:build` builds the issue's PRD on an `<ID>-<feature>` branch in a worktree under this run's directory, then the branch is pushed and a PR opened and linked from the issue.

## Board

- Issues assigned to your role account, in any project; the principles file named in the prompt holds the rules every role and task shares.
- Statuses: Todo (queue) → In Progress → In Review (needs the user: PR ready, questions, build failed, or a bad `Repo:` line) → Done (user only). Canceled only as in step 2.

## Inputs

- The cwd is this run's directory `work/<ID>/` in the agent-pm root; `work/<ID>/…` below is a path in it, passed as an absolute path. The prompt's `Repo check: OK` line gives `<owner>/<name>`, `<clone>`, `<default>` (the default branch), `<branch>` and `<worktree>` (`work/<ID>/worktrees/<branch>`).
- `<eng> status` and `<eng> comments --since <ISO>` print JSON, where `<eng>` is exactly the prompt's `eng.py:` command (`python3 <ROOT>/scripts/eng.py`; the launcher sets `AGENT_PM_ISSUE`). `status`: `repo`, `clone`, `default`, `branch`, `worktree`, `pr_title`, `worktrees_dir_ok`, `branch_exists` (`local`/`remote`/null), `worktree_exists`, `pr` (number, url, state of the branch's non-fork PR by the user's `gh` login, or null), `plan_docs` (path, phase). `comments`: the PR comments and reviews after `<ISO>` written by the user's `gh` login (`kept`); others are dropped and counted. Exit 2 = refused or malformed `gh` output (reason on stderr), 3 = transient.
- A Handoff-created issue (principles) comes from the PRD issue: `## Source` holds the PRD link and `## Instructions` the `Repo:` line and which phase to build.

## Steps

1. **Read** the issue, all its comments, the PRD from `## Source` (local `~/playground/private_docs` clone), `## Instructions`. The user's comments outrank `## Instructions` and the PRD.
2. **Repo.** From the prompt's `Repo check` line; never inspect paths outside the run's dirs (the cwd, `<clone>`, `<worktree>`, `~/playground/private_docs`). On `Repo check failed: <reason>`, bounce and stop:
   - Description starts with `Handoff from <ID>:` → on that PRD issue comment the reason and ask the user to Handoff again with a correct `Repo:` comment, move it to In Review; on this issue comment the same and move it to Canceled.
   - Otherwise → comment `Question:` with the reason, asking the user to fix the `Repo:` line in the description and then move the issue back to Todo; move the issue to In Review.
3. **Status.** `<eng> status`; if it shows a PR, also `<eng> comments --since <the issue's creation time>` (its `kept` comments count as the user's).
4. **Which build.** From `plan_docs` (autopilot's plan doc and its `RESUME: phase=` line) and the comments:
   - A plan doc in the worktree with phase before S9 → continue that build (`autopilot:build` resumes from its plan doc), with the user's comments as requirements: update the spec and plan where they differ, then continue.
   - Else a finished build and a user comment (issue or PR) is newer than the latest `Build started` comment → a new build with the user's comments as the requirement (new plan doc).
   - Else a finished build → comment `Question:` asking what to do next, move to In Review, stop.
   - No build yet → a new build of the PRD's first phase (or the phase `## Instructions` names).
5. **Start.** On a new (not resumed) run, comment `Build started`.
6. **Build.** If `<eng> status` shows `worktrees_dir_ok: false`, fail (step 8) without building. Run `autopilot:build` with a requirement stating, with the values filled in:
   - the PRD path, `## Instructions` and the user's comments, with step 1's precedence, and the charter's conventions standard and git boundary, copied into the requirement with `<default>` as the default branch;
   - "use the worktree `<worktree>` on branch `<branch>`: if it does not exist, `git -C <clone> fetch origin`, then `git -C <clone> worktree add -b <branch> <worktree> origin/<default>` for a new branch, `git -C <clone> worktree add <worktree> <branch>` for an existing local one, or `git -C <clone> worktree add --track -b <branch> <worktree> origin/<branch>` for a remote-only one; then work only there, with absolute paths; create no other worktree or branch";
   - "put the spec and plan doc where the target repo keeps design docs (e.g. an existing `docs/specs/`), else in `autopilot_docs/` at the repo root; commit them on the branch";
   - "skip S8: keep the commits"; "after each implementation task and each review round run exactly `git -C <worktree> push -u origin <branch>`".
7. **PR** (build converged):
   - `git -C <worktree> push -u origin <branch>`.
   - Comment `Build docs:` with the GitHub links of the build's spec and plan doc on the branch (`https://github.com/<owner>/<name>/blob/<branch>/<path>`, paths from `<eng> status`; the spec is the plan doc's `spec_file=`).
   - Write `work/<ID>/pr.md`: what changed, PRD and spec links, how to verify, residual non-blocking items.
   - If `<eng> status` shows no PR, `gh pr create --repo <owner>/<name> --head <branch> --base <default> --title '<pr_title>' --body-file work/<ID>/pr.md`, else `gh pr edit <number> --repo <owner>/<name> --body-file work/<ID>/pr.md`.
   - Attach the PR URL to the issue; subscribe the humans (principles); comment `Build ready:` + 3–5 lines incl. how to verify.
   - The GitHub integration moves the issue to In Review when the PR opens, so do not move it: read its state and move it to In Review only if it is not there (e.g. the PR already existed, or the integration lagged).
   - Fallbacks: push denied → comment `Build failed:` "push not permitted" and move to In Review; PR creation denied → attach `https://github.com/<owner>/<name>/compare/<default>...<branch>?expand=1` and say in `Build ready:` that the user must open the PR.
8. **Failure** (build stopped or capped, or an action it needs was denied by the permission classifier): push the branch (`git -C <worktree> push -u origin <branch>`); comment `Build docs:` with the spec and plan links as in step 7 (whatever exists); comment `Build failed:` with the failing tests, open blockers or denied action, and the branch URL (`https://github.com/<owner>/<name>/tree/<branch>`); move to In Review.

## Resume rule

A prompt starting "Resumed run" continues this session after an interruption. Re-read this file first; it overrides any earlier resume rule in your context. Use `<eng> status` and this session's history to find what is done and do only the rest (step 4 decides the build). Never repeat `Build started`, never re-create a branch, worktree or PR, and never move the issue to Todo.
