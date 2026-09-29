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

The existing Edit deny rules `roles/**` and `tasks/**` already cover the new `.toml` files, so a run cannot edit its own role or task settings.

## Loading (`pipeline.py`)

`load_config(path)` (every consumer, incl. promote) keeps only the `next`-chain checks; it no longer touches roles or tasks (the `allowed_tools` check moves to the task loader).

`runnable(cfg, root=ROOT)` (router and launcher) raises `SystemExit("<file>: …")`, where `<file>` is `pipeline.toml`, `roles/<name>.toml` / `.md` or `tasks/<name>.toml` / `.md` (relative to `root`), when:

1. `pipeline.toml` has a top-level key other than `team`, `human_members`, `projects`, or a project has a key other than `next`, `prefix`, `require_instructions`, `role`, `task` (the message lists the keys and says role and task settings live in `roles/<role>.toml` / `tasks/<task>.toml`); this replaces the phase-1 moved-key check;
2. a project has exactly one of `role` / `task`, or names a role / task with no pair;
3. pairs — `root/roles` and `root/tasks` are scanned (regular files ending in `.md` or `.toml`; other files are ignored): a `<name>.md` without `<name>.toml` or the reverse; `roles/principles.md` is the one exception (not a role) and `roles/principles.toml` is rejected;
4. a scanned name is not lowercase kebab (`[a-z0-9]+(-[a-z0-9]+)*`);
5. a `.toml` file is not valid TOML (the decode error is in the message);
6. a role file has a key other than `read_only` / `memory`, or a task file a key other than `model` / `effort` / `add_dirs` / `repo_from_issue` / `allowed_tools`, or a task lacks `model` or `effort`;
7. `read_only` and `memory` — as in phase 1 (list of absolute paths or `{repo}`; memory an existing absolute directory, compared by `realpath`: not at, under or an ancestor of `root` or of the role's non-`{repo}` `read_only` paths), except that `~/.claude` and `~/Library/LaunchAgents` are now checked in both directions: memory at, under or an ancestor of either fails (so `~/Library` and `~` fail);
8. `allowed_tools` fails `check_allowed_tools` (rule set unchanged; its messages take the task file as prefix);
9. a role whose `read_only` has `{repo}` is paired in a runnable project with a task lacking `repo_from_issue` or having `allowed_tools`.

Every scanned pair is validated, referenced or not, so a broken or orphaned file fails at deploy. Overlap is one shared helper, `overlaps(a, b)` (realpaths, either at or under the other), used by check 7 and the launcher.

`Run` keeps `task_name`, `task` (the task file's table), `charter`, `instructions`, `memory`, `read_only`; `project`, `role_name` and `role` are dropped. Nothing reads them today (the router uses only `runnable()`'s keys; the launcher uses `a.project`).

## Launcher (`launch.py`)

After the repo step returns Ok or Invalid (a Transient still exits 3 first) and before `os.makedirs(run_dir)` and tmux, for a run whose role has `memory`: the `{repo}` paths are `work/<ID>/worktrees` and, when Ok, the clone. If `overlaps(memory, path)` for any of them, the launcher writes `<ts> config-error <ID>: role memory <memory> overlaps <path>; Edit deny rules would make it unwritable` to the project log and stderr and exits 2 with no run. This holds for new and resumed runs, and whether or not the role's `read_only` has `{repo}` (decision below). The router's handling of exit 2 is unchanged (it logs the exit code; Recover treats the claimed issue as a run that never started).

`launch.main` gains `root=ROOT`, passed to `runnable`, so tests can load a temporary role/task registry; prompts and deny rules still take their paths from `Run` and `ROOT` as in phase 1.

## Decisions

- Validate every pair found in `roles/` / `tasks/`, not only referenced ones: an orphaned `.md` or `.toml` is the exact failure the request names, and it must fail at deploy.
- Checks stay in `runnable()`, so promote (which calls only `load_config`) keeps working with a half-migrated registry, as in phase 1.
- The launcher's memory-vs-repo check applies to every role with `memory` on a `repo_from_issue` task, not only roles with `{repo}` in `read_only`: a memory dir inside the target clone or worktrees would also leak into the build's commits; the stricter rule is simpler and loses nothing.

## Docs

- `README.md`: `roles/` and `tasks/` bullets describe the pairs and their keys (moved from the `pipeline.toml` bullet, which keeps `team`, `human_members`, `projects`); the memory rule says "not overlapping" `~/.claude` / `~/Library/LaunchAgents`; the launcher bullet adds the config-error exit 2; the Design list adds this spec.
- `CLAUDE.md`: the registry sentence describes the pairs and the three `pipeline.toml` keys; `runnable()` rejects unpaired files, unknown keys and the overlaps; the launcher's memory-vs-repo check.
- `docs/specs/2026-09-28-task-44-role-task-design.md`: one line under the title: "Amended by `2026-09-29-task-44-role-task-pairs-design.md`: role and task settings live in `roles/<role>.toml` / `tasks/<task>.toml`."

## Testing

- `test_pipeline.py` (temporary `root` with `.md` + `.toml` pairs): each check 1–9 fails with the right file prefix, incl. orphan `.md`, orphan `.toml`, `roles/principles.toml`, bad TOML, `[roles]` / `[tasks]` left in `pipeline.toml`, a moved project key, and memory = an ancestor of `~/.claude` or `~/Library/LaunchAgents` (e.g. `$HOME/Library`) and at/under them; a valid registry yields the expected `Run`s (dropped fields absent); `load_config` ignores role/task files; the real repo yields the three runs as before.
- `test_launch.py`: memory at, under and an ancestor of the Ok clone, and under `work/<ID>/worktrees` → exit 2, a `config-error` line in the project log, no tmux call; the same for a resumed run; a disjoint memory starts the run; the real-config NFR-1 argv tests pass unchanged.
- `test_router.py`: fixtures drop `[roles]` / `[tasks]`; behavior unchanged.
- Verification: `python3 -m unittest discover -s scripts/tests`, `python3 scripts/router.py --now --dry-run`, `python3 scripts/promote.py --dry-run` from the worktree.
