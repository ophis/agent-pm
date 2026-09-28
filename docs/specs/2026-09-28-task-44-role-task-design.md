# TASK-44: Role + task restructure (phase 1) — design

Source: PRD `private_docs/Product Design/2026-09-28-1201-TASK-32-role-capability-restructure.md`, phase 1 = FR-1…FR-12 with NFR-1…NFR-3. Out of scope: phase 2 (FR-13…FR-16, `Type`-label routing), the dispatcher rename (A4), the capability layer (D1), memory mechanics (TASK-33), and running the deployment itself (FR-11 is delivered as documented steps).

## Goal

Split today's single "stage" concept into **role** (identity: charter, memory dir, read-only scope) and **task** (procedure: instructions file, model, effort, dirs). Phase 1 routes by project only: each runnable project names one `role` and one `task`; the launcher resolves both. The three existing runs keep their behavior (NFR-1).

## Files (FR-1, FR-2, FR-2a, FR-3)

`git mv` (history kept):

| From | To |
| -- | -- |
| `stages/principles.md` | `roles/principles.md` |
| `stages/deep-research.md` | `tasks/deep-research.md` |
| `stages/product-design.md` | `tasks/product-design.md` |
| `stages/engineering.md` | `tasks/engineering.md` |

`stages/` no longer exists. `tasks/` sits beside `templates/` and `scripts/`, so `../templates/prd.md` and `../scripts/router.py` (relative to a task file) stay valid.

New charters `roles/researcher.md`, `roles/pm.md`, `roles/engineer.md`, each exactly:

```markdown
# <Role title>

<one responsibility paragraph, taken from the matching task file's opening paragraph, phrased for the role>

## Memory
```

The Memory section stays empty (FR-2a). Charters add no rules: no boundaries section in phase 1, and nothing repeated from `principles.md` or the task file.

Text edits (no other rule changes):
- `roles/principles.md`: title `# Principles (every role and task)`; intro states the three documents the prompt names (these principles, the role charter = identity: responsibilities, boundaries, memory; the task = the steps) and the precedence on conflict: principles > the charter's boundaries > the task's steps. "stage" wording becomes role/task (e.g. "the run's project id is the prompt's `Project:` value"). The existing bullets are otherwise unchanged.
- Each task file: "`principles.md` beside this file holds the rules every stage shares" → "the principles file named in the prompt holds the rules every role and task shares". Nothing else changes in task files.

## Registry (FR-4)

`pipeline.toml` after migration:

```toml
team = "Frank's Agents"
human_members = ["ophis.w@outlook.com"]

[roles.researcher]
[roles.pm]
[roles.engineer]
read_only = ["~/playground/private_docs"]   # the PRDs; the engineer only reads them

[tasks.deep-research]
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]

[tasks.product-design]
model = "opus"
effort = "high"
add_dirs = ["~/playground/private_docs"]

[tasks.engineering]
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]
repo_from_issue = true

[projects."03495382-…"]  # 1-Research
next = "ba0738ba-…"
role = "researcher"
task = "deep-research"
# … Product Design: prefix, next, require_instructions = false, role = "pm", task = "product-design"
# … Engineering: prefix = "ENG", role = "engineer", task = "engineering"
```

Keys:
- `[roles.<role>]`: `read_only` (optional list), `memory` (optional path). Charter file `roles/<role>.md`.
- `[tasks.<task>]`: `model`, `effort` (required), `add_dirs`, `repo_from_issue`, `allowed_tools` (same meaning as the former project keys). Instructions file `tasks/<task>.md`.
- `[projects."<id>"]`: `role` + `task` make it runnable (replacing `instructions`); `next`, `prefix`, `require_instructions` stay here.

## Validation (FR-5, NFR-2)

`pipeline.load_config` (all consumers, including promote) keeps the `next` checks and runs `check_allowed_tools` over each `[tasks]` entry instead of each project (the rule set itself is unchanged; `repo_from_issue` is read from the task).

`pipeline.runnable(cfg, root)` (router and launcher only; promote does not call it) raises `SystemExit("pipeline.toml: …")` when:
1. any project (runnable or not) still has a moved key: `instructions`, `model`, `effort`, `add_dirs`, `repo_from_issue`, `allowed_tools`;
2. a project has exactly one of `role` / `task`;
3. a referenced role or task has no `[roles.<name>]` / `[tasks.<name>]` entry, or its file `roles/<name>.md` / `tasks/<name>.md` under `root` is missing;
4. a role or task name (any declared key, and any referenced name) is not lowercase kebab (`[a-z0-9]+(-[a-z0-9]+)*`), or a role is named `principles`;
5. a declared task lacks `model` or `effort`;
6. a `read_only` entry is neither exactly `{repo}` nor an absolute path after `~` expansion;
7. a role whose `read_only` contains `{repo}` is paired (in some runnable project) with a task that lacks `repo_from_issue` or has `allowed_tools`;
8. a role's `memory` is not an absolute path after `~` expansion, is not an existing directory, or lies at/under one of that role's non-`{repo}` `read_only` paths or under `root/roles`, `root/tasks`, `root/templates` (compared by `realpath`).

Checks 3–8 cover every declared `[roles]` / `[tasks]` entry, not only referenced ones, so a broken entry fails at deploy (NFR-2). `runnable` returns `{project_id: Run}` for projects with `role` + `task`, where `Run` bundles the project entry, role name + entry, task name + entry. The router uses only the keys, as today.

## Launcher (FR-6…FR-9)

Router and log formats are unchanged; `launch.py --project` still selects the entry (FR-6). On resume, the project — hence role and task — is the same as at start.

Paths: `P = ROOT/roles/principles.md`, `C = ROOT/roles/<role>.md`, `T = ROOT/tasks/<task>.md`, `M` = the role's expanded `memory` (absent when unset). All absolute.

Prompt (FR-7), before the existing ` Reviewer: … Humans: … Project: …` tail and the repo-step tail:
- new: `Follow {P}, your role charter {C} and the task {T} to handle {ID} ({URL}). The runner has already claimed it.`
- resume: `Resumed run {k} for {ID} ({URL}) after an interruption. Re-read {P}, your role charter {C} and the task {T} first (they may have changed since this session started) and follow the task's resume rule.`
- when `M` is set, both append ` Your role memory: {M}; your role charter says how to use it.`

Command (FR-8), in this order:
- `--add-dir` `ROOT/roles`, `ROOT/tasks`, `ROOT/templates`, then each task `add_dirs` (expanded), then `M` if set;
- `--disallowedTools` `Edit(//ROOT/roles/**)`, `Edit(//ROOT/tasks/**)`, `Edit(//ROOT/templates/**)`, `Edit(//<run_dir>/worktrees/*/.git)`, then for each role `read_only` entry: an absolute path `X` → `Edit(//X/**)`; `{repo}` → `Edit(//<clone>/**)` (only when the repo step returned Ok) and `Edit(//<run_dir>/worktrees/**)`;
- the removed rule "for `repo_from_issue` deny every `add_dirs`" is now expressed by `engineer.read_only`, with the same resulting rule;
- model, effort, `--permission-mode auto`, `--setting-sources user`, `--strict-mcp-config`, `--allowedTools` (from the task's `allowed_tools`) and the repo step are unchanged, keyed off the task entry.

Log (FR-9): `logs/projects/<task>.log` — same names as today (`deep-research`, `product-design`, `engineering`).

## NFR-1 check

Versus today, each of the three runs' `claude` argv differs only in: prompt text (charter path added, principles path moved, resume wording "stage's" → "task's"), and `stages/` replaced by `roles/` + `tasks/` in `--add-dir` and `--disallowedTools`. Model, effort, other dirs, Edit rules, env, tmux, and log names are identical.

## Docs (FR-10, FR-12)

- `CLAUDE.md` and `README.md`: describe roles/tasks, the three registry sections, the new prompt, `--add-dir`/deny sets, and log naming; drop `stages/` and `instructions`.
- FR-12 needs no repo change (the PRDs in `private_docs` are not revised).
- Older `docs/specs/*` are historical and stay as written.

## Deployment (FR-11, documented only)

Delivered in the PR description: deploy outside 02:00–06:59, with no tmux session `agent-pm` and no In Progress issue assigned to the agent in a runnable project; then `python3 -m unittest discover -s scripts/tests`, `python3 scripts/router.py --now --dry-run`, `python3 scripts/promote.py --dry-run` all pass, else `git revert` the merge. `work/<ID>/` and transcript locations do not change, so existing sessions stay resumable.

## Testing

- `test_pipeline.py`: each validation 1–8 (fails) plus a valid config (passes); `load_config` checks `allowed_tools` on tasks; promote-style `load_config` ignores missing role/task files; the real `pipeline.toml` yields the three expected runs.
- `test_launch.py`: new/resume prompts with and without memory; `--add-dir` and deny sets; `{repo}` expansion for Ok and Invalid; log file named after the task; and NFR-1: for each of the three real projects in the repo's `pipeline.toml`, the full `claude` argv is asserted (Engineering with a mocked Ok repo step).
- `test_router.py` / `test_promote.py`: fixtures migrated to `role` / `task`; behavior unchanged.
- Verification: `python3 -m unittest discover -s scripts/tests`, `python3 scripts/router.py --now --dry-run`, `python3 scripts/promote.py --dry-run` from the worktree (dry-run only; changes nothing).
