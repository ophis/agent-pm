# TASK-44: Role and task file pairs — design

Amends `docs/specs/2026-09-28-task-44-role-task-design.md` (phase 1) per the user's change request on PR #2. Everything not named here keeps that spec's behavior; in particular the three runs' `claude` argv, prompts, log names and the router are unchanged (NFR-1).

## Change request (source)

1. A role is a pair: `roles/<role>.md` (charter, for the LLM; the current placeholders stay) + `roles/<role>.toml` (`read_only`, `memory`; may be empty); one without the other fails loading. `roles/principles.md` stays as is and has no `.toml`.
2. A task is a pair: `tasks/<task>.md` (steps) + `tasks/<task>.toml` (`model`, `effort`, `add_dirs`, `repo_from_issue`, `allowed_tools`); one without the other fails loading.
3. `pipeline.toml` keeps only `team`, `human_members` and `projects` (`next`, `prefix`, `require_instructions`, `role`, `task`).
4. The memory-dir check rejects overlap with `~/.claude` and `~/Library/LaunchAgents` in both directions (memory = `~/Library` fails).
5. Drop the unused `Run` fields `project`, `role_name`, `role`.
6. After the repo resolves and before starting Claude, the launcher rejects a role memory dir that overlaps the resolved `{repo}` paths (the clone or `work/<ID>/worktrees`): a config error (exit 2, reason in the project log), tested.
7. Update tests, README and CLAUDE.md.

## Files

New, contents moved verbatim from today's `[roles.*]` / `[tasks.*]` tables (comments kept):

| File | Content |
| -- | -- |
| `roles/researcher.toml`, `roles/pm.toml` | empty |
| `roles/engineer.toml` | `read_only = ["~/playground/private_docs"]   # the PRDs` |
| `tasks/deep-research.toml` | `model = "opus"`, `effort = "xhigh"`, `add_dirs = ["~/playground/private_docs"]` |
| `tasks/product-design.toml` | `model = "opus"`, `effort = "high"`, `add_dirs = ["~/playground/private_docs"]` |
| `tasks/engineering.toml` | `model = "opus"`, `effort = "xhigh"`, `add_dirs = ["~/playground/private_docs"]`, `repo_from_issue = true` |

`pipeline.toml` loses its `[roles]` / `[tasks]` tables and their comments; its header comment says a project's `role` / `task` name the pairs `roles/<role>.md` + `.toml` and `tasks/<task>.md` + `.toml`. Charters, task `.md` files and `roles/principles.md` are unchanged.

The `.toml` files hold run-permission settings (`read_only`, `memory`, `add_dirs`, `allowed_tools`) inside `roles/` and `tasks/`, which every run gets as `--add-dir`. The existing Edit deny rules stop only the file-editing tools, not a Bash write, so the router refuses to start any run while those directories have uncommitted changes (see Router). Residual: a run that commits in the live clone, or writes `pipeline.toml` via Bash (as today), is not caught; both remain correctness aids, not a security boundary.

## Loading (`pipeline.py`)

`load_config(path)` (every consumer, incl. promote) keeps only the `next`-chain checks; it no longer touches roles or tasks (the `allowed_tools` check moves to the task loader).

`runnable(cfg, root=ROOT)` (router and launcher) raises `SystemExit("<file>: …")`, where `<file>` is `pipeline.toml`, `roles/<name>.toml` / `.md` or `tasks/<name>.toml` / `.md` (relative to `root`), when:

1. `pipeline.toml` has a top-level key other than `team`, `human_members`, `projects`, or a project has a key other than `next`, `prefix`, `require_instructions`, `role`, `task` (the message lists the keys and says role and task settings live in `roles/<role>.toml` / `tasks/<task>.toml`); this replaces the phase-1 moved-key check;
2. a project has exactly one of `role` / `task`, or names a role / task with no pair (so `role = "principles"` fails);
3. pairs — `root/roles` and `root/tasks` are scanned (regular files ending in `.md` or `.toml`; other files are ignored): a `<name>.md` without `<name>.toml` or the reverse; `roles/principles.md` is the one exception (not a role) and `roles/principles.toml` is rejected;
4. a scanned name is not lowercase kebab (`[a-z0-9]+(-[a-z0-9]+)*`);
5. a `.toml` file is not valid TOML (the decode error is in the message);
6. a role file has a key other than `read_only` / `memory`, or a task file a key other than `model` / `effort` / `add_dirs` / `repo_from_issue` / `allowed_tools`, or a task lacks `model` or `effort` (as in phase 1);
7. `read_only` and `memory` — as in phase 1 (list of absolute paths or `{repo}`; memory an existing absolute directory, compared by `realpath`: not at, under or an ancestor of `root` or of the role's non-`{repo}` `read_only` paths), except that `~/.claude` and `~/Library/LaunchAgents` are now checked in both directions: memory at, under or an ancestor of either fails (so `~/Library` and `~` fail);
8. `allowed_tools` fails `check_allowed_tools` (rule set unchanged, still compared against the deploy `ROOT`, not `runnable`'s `root`; its messages take the task file as prefix);
9. a role whose `read_only` has `{repo}` is paired in a runnable project with a task lacking `repo_from_issue` or having `allowed_tools`.

Every scanned pair is validated, referenced or not, so a broken or orphaned file fails at deploy. Scanning and per-file checks (3–8) live in one helper that returns the validated roles and tasks; `runnable` checks `pipeline.toml` (1, 2) and pairs projects with them (9). Overlap is one shared helper, `overlaps(a, b)` (realpaths, either at or under the other), used by check 7 and the launcher.

`Run` keeps `task_name`, `task` (the task file's table), `charter`, `instructions`, `memory`, `read_only`; `project`, `role_name` and `role` are dropped. Nothing reads them today (the router uses only `runnable()`'s keys; the launcher uses `a.project`).

## Launcher (`launch.py`)

**Memory vs repo** (new and resumed runs). Only for a `repo_from_issue` task whose role has `memory`, after the repo step returns Ok or Invalid (a Transient still exits 3 first) and before `os.makedirs(run_dir)` and tmux. The `{repo}` paths are `work/<ID>/worktrees` and, when Ok, the clone. If `overlaps(memory, path)` for any of them, the launcher writes `<ts> config-error <ID>: role memory <memory> overlaps the issue's repo <path>` to the project log and stderr and exits 2 with no run (the same shape as today's `transient` line, exit 3). This holds whether or not the role's `read_only` has `{repo}` (decision below). With the real `ROOT`, check 7 already keeps memory away from `work/`, so the clone is the half that can trigger in production; the worktrees half is kept because the request names it. Other tasks skip this check.

Exit 2 reaches the user through the unchanged router: it logs the exit code, the claimed issue stays In Progress with no transcript, and Recover retries it until the attempt cap moves it to In Review with the cap comment; the reason is in the project log. This is intended (the request asks for exit 2), although the error depends on the issue's `Repo:` line.

`launch.main` gains `root=ROOT`, passed only to `runnable`, so tests can load a temporary role/task registry; `PRINCIPLES`, `--add-dir`, deny rules and `run_dir` still use `ROOT` / `WORK` as in phase 1.

## Router (`router.py`)

**Uncommitted registry.** Each tick (incl. `--dry-run`), after the tmux check and before prune, Recover and any claim or resume, runs `git -C ROOT status --porcelain -z --ignored -uall -- roles tasks` through the tick's injectable `sh`. Any tracked change (modified, staged, deleted, renamed), or any untracked or ignored file ending in `.md` / `.toml`, stops the tick: `SystemExit("roles/ or tasks/ has uncommitted changes: <paths>")`, the same fail-loud path as a `runnable()` error; the git call failing stops it too. No issue is claimed, moved or launched. Other untracked or ignored files (`.DS_Store`, editor swap files) are skipped, matching what check 3 scans; `--ignored` catches a `.gitignore` written to hide a new file. It always reads the deploy `ROOT` (the router has no other root). The parsing of the porcelain output is a pure `pipeline` function.

## Decisions

- Validate every pair found in `roles/` / `tasks/`, not only referenced ones: an orphaned `.md` or `.toml` is the exact failure the request names, and it must fail at deploy.
- Checks stay in `runnable()`, so promote (which calls only `load_config`) keeps working with a half-migrated registry, as in phase 1.
- The launcher's memory-vs-repo check applies to every role with `memory` on a `repo_from_issue` task, not only roles with `{repo}` in `read_only`: a memory dir inside the target clone or worktrees would also leak into the build's commits; the stricter rule is simpler and loses nothing.
- The uncommitted-registry guard lives in the router before any claim: it is a deployment-wide condition, and failing after a claim would burn every queued issue's attempts. Deploys are `git pull`, so a clean `roles/` / `tasks/` is the normal state.

## Docs

- `README.md`: `roles/` and `tasks/` bullets describe the pairs and their keys (moved from the `pipeline.toml` bullet, which keeps `team`, `human_members`, `projects`); the memory rule says "not overlapping" `~/.claude` / `~/Library/LaunchAgents`; the launcher bullet adds the memory-vs-repo config error (exit 2); the router bullet adds the uncommitted-registry stop; the Design list adds this spec.
- `CLAUDE.md`: the registry sentence describes the pairs and the three `pipeline.toml` keys; `runnable()` rejects unpaired files, unknown keys and the overlaps; the launcher's memory-vs-repo config error; a gotcha: uncommitted `.md` / `.toml` edits under `roles/` / `tasks/` in the live clone stop every router tick.
- `docs/specs/2026-09-28-task-44-role-task-design.md`: one line under the title: "Amended by `2026-09-29-task-44-role-task-pairs-design.md`: role and task settings live in `roles/<role>.toml` / `tasks/<task>.toml`."

## Testing

- `test_pipeline.py` (temporary `root` with `.md` + `.toml` pairs): each check 1–9 fails with the right file prefix, incl. orphan `.md`, orphan `.toml`, `roles/principles.toml`, bad TOML, `[roles]` / `[tasks]` left in `pipeline.toml`, a moved project key, and memory = an ancestor of `~/.claude` or `~/Library/LaunchAgents` (e.g. `$HOME/Library`) and at/under them; a valid registry yields the expected `Run`s (dropped fields absent); `load_config` ignores role/task files; the real repo yields the three runs as before.
- `test_launch.py`: memory at, under and an ancestor of the Ok clone, and under `work/<ID>/worktrees` (with the Invalid result too) → exit 2, a `config-error` line in the project log, no tmux call; the same for a resumed run; a disjoint memory starts the run; a non-`repo_from_issue` task with memory skips the check; the real-config NFR-1 argv tests pass unchanged.
- `test_router.py`: fixtures drop `[roles]` / `[tasks]`; the fake shell answers the git call clean by default; a dirty registry (tracked change, untracked `.toml`, ignored `.md`) or a failing git call stops the tick — also with `--dry-run` — before any Linear mutation or launch; an untracked `.DS_Store` alone does not. `test_pipeline.py` covers the porcelain parser (incl. a rename entry and a path with spaces).
- Verification: `python3 -m unittest discover -s scripts/tests`, `python3 scripts/router.py --now --dry-run`, `python3 scripts/promote.py --dry-run` from the worktree.
