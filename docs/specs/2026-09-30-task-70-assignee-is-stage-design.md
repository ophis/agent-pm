# TASK-70: the assignee is the stage — design

Source: TASK-70 (https://linear.app/ophis-workgroup/issue/TASK-70), PR-C of the TASK-62 PRD (`private_docs/Product Design/2026-09-29-1540-TASK-62-board-model-products-roles-tasks.md`, 实施顺序 step 6; FR-3, FR-4, FR-5, FR-8–FR-13, FR-18, FR-23). Builds on PR-A (TASK-67: `roles/<role>.toml` `tasks`/`account`/`key`, task `prefix`, role keys, FR-11) and PR-B (TASK-69: In Review subscribes the humans, keeps the assignee). Merged, then deployed at the cutover (PRD step 7).

Model: a Linear project is a product; an issue's assignee (a role account) is its stage; the role runs its default task (first in `tasks`).

## Goals

1. **G1 — Config (FR-3).** `pipeline.toml` holds only `team`, `human_members`, `[states]`, `harness_key` and a `[roles.<role>]` table per role with optional `next` and `require_instructions`. `[projects]` (with its `prefix`, `role`, `task`) goes. Loading rejects a `next` naming an undefined role and a `next` role whose default task has no `prefix`. Values: researcher → pm → engineer; engineer has no `next`; pm has `require_instructions = false`.
2. **G2 — Queue (FR-4) and order (FR-5).** The router's queue is the team's Todo issues that are in a project and assigned to a role account. Order: priority, then the later role in the `next` chain, then the older issue.
3. **G3 — Assignee untouched (FR-8, FR-10).** Claiming only moves the issue to In Progress; no harness state change (claim, Recover requeue, attempt cap, promote's moves) sets `assigneeId`.
4. **G4 — Recover (FR-9).** Recover scans the team's In Progress issues assigned to role accounts; requeuing to Todo keeps the assignee.
5. **G5 — FR-11 kept.** Only `human_members` moves to Todo reset the attempt cap (unchanged).
6. **G6 — Manual pick (FR-12).** `router.py --pick [--role ROLE] [RUNS_LOG]` replaces `--pick [--project ID]`; `tasks/deep-research.md` step 1 uses `--pick --role researcher`.
7. **G7 — Launcher (FR-13, rest).** The launcher resolves the role from the issue's assignee and runs its default task; no project lookup.
8. **G8 — Hand-off (FR-18).** Promote scans the team's Handoff issues in a project and assigned to a role account. If that role has a `next`, it creates the child in the source's project, assigned to the next role's account, titled `<next role's default task prefix>: <source title without the source role's default task prefix>`. Idempotent id, source link, instructions, relation and moving the source to Done are unchanged.
9. **G9 — Docs (FR-23).** README and CLAUDE.md describe the board model (project = product, assignee = stage) and the cutover steps; rules and docstrings drop the stage-project model.

## Non-goals

- Tasks by label (TASK-64), target repo by project (TASK-65; Engineering keeps the `Repo:` line), cross-role work (TASK-35), migrating existing issues out of the stage projects (PRD open question 1).
- New validation, config or abstraction beyond what the listed items need (prototype): e.g. no check that two roles have distinct `account`s, no re-check of the assignee at claim time.
- Historical docs in `docs/specs/`.

## Decisions

- **Launcher input: `--assignee <email>`** (the assignee's email as the router read it). The launcher maps it to the role whose `account` matches case-insensitively, as FR-13 states. The router matched the same accounts by user id resolved case-insensitively, so the launcher's match cannot miss for an issue the router picked. `--project <id>` stays, only for the prompt's `Project:` value (now the issue's product project).
- **Role accounts are matched by Linear user id.** A shared helper resolves each role's `account` with the existing case-insensitive `user_id()`; an account not found in Linear stops the caller, as `humans()` does (it replaces the old "runnable projects not found in Linear" check). Linear's `in` comparator is case-sensitive, and promote needs the next role's id for `assigneeId` anyway.
- **Child id key: the next role's name.** `child_id(source_id, next_role, handoff_at)` in place of the next project id: the project is now the source's own, so it no longer tells the hop apart. A Handoff the old code left half-promoted (child created, source not Done) is not found under the new key; promote retries every tick, so at the cutover's stop none is expected — accepted.

## G1 — Config

`pipeline.toml` after the change:

```toml
team = "…"; human_members = […]; harness_key = "linear-api-key"   # unchanged
[states]   # unchanged

# Each role's place in the pipeline, keyed by role name (roles/<role>.md + roles/<role>.toml).
[roles.researcher]
next = "pm"                      # promote hands its Handoff issues to this role

[roles.pm]
next = "engineer"
require_instructions = false     # a PRD carries enough context; Handoff comments are optional

[roles.engineer]                 # no next: the last stage
```

`scripts/pipeline.py`:

- `TOP_KEYS` = `{team, states, human_members, harness_key, roles}`; `PROJECT_KEYS` becomes `PIPELINE_ROLE_KEYS = {"next", "require_instructions"}`. A leftover `[projects]` table is an unknown top-level key (existing check).
- `load_config`: `cfg.setdefault("roles", {})`; the next-chain cycle check runs over `cfg["roles"]` (kept). The project `prefix` check goes (replaced below); its docstring points at `runnable()` for the role checks, which every consumer (router, launcher, promote) calls.
- `Run` gains `account` (the role's Linear email). `runnable(cfg, root)` returns `{role name: Run}` for every role in `registry(root)`, its `task_name` being the role's default task (`tasks[0]`); charter `roles/<role>.md`, instructions `tasks/<task>.md`. Checks (fail loud, `SystemExit`):
  - unknown top-level keys (kept);
  - a role's key equal to `harness_key` (kept);
  - `[roles.<name>]` whose name has no `roles/<name>.md` + `.toml` pair; unknown keys in a `[roles.<name>]` table (these replace the project `role`/unknown-key checks);
  - `next` naming a role with no pair: `pipeline.toml: next of 'X' names undefined role 'Y'`;
  - `next` naming a role whose default task has no `prefix`: `pipeline.toml: next of 'X' is role 'Y', whose default task 'T' has no prefix`;
  - the `{repo}` read_only rule (kept), applied to each role's default task.
- `stage_order(cfg)` returns `{role: position in its next chain}` over `cfg["roles"]` (same algorithm); roles outside a chain are 0.
- New `role_ids(gql, runs)`: `{Linear user id: role name}` for the given `{role: Run}`, via `user_id()`; an account not found stops the caller: `roles/<role>.toml: account 'E' not found in Linear`.
- `Team` drops `projects`, and `Q_TEAM` its `projects` sub-query (only the old router/promote checks used them).

## G2–G6 — Router (`scripts/router.py`)

- `Board(gql, entries, tdir, now, dry, cfg, only=None)`, `only` a role name. `runs = runnable(cfg)`; `only` not a role → `SystemExit("no role 'X' in roles/")`. `self.roles = role_ids(gql, runs restricted to only)`; `self.stage = stage_order(cfg)`. The `viewer` query and `self.me`, `self.projects` and the project checks go.
- `issues(state)` filter: `{"team": {"id": {"eq": <team id>}}, "project": {"null": False}, "assignee": {"id": {"in": [ids of self.roles]}}, "state": {"id": {"eq": <state id>}}}`; each node also selects `assignee { id email }`. Unassigned issues, other assignees and issues without a project never appear.
- `later(issue)` = `-self.stage.get(self.roles[issue["assignee"]["id"]], 0)`. `take`'s sort key (`rank`, `later`, `createdAt`) is unchanged.
- Claim: `issueUpdate(id, input: { stateId: <in_progress> })`, no `assigneeId`.
- Recover: `self.issues("in_progress")` (no viewer filter); both requeues call `comment_and_move(issue, INTERRUPTED, "todo")` without `assigneeId`; `comment_and_move` drops its `**extra`.
- `tick` launches `launch.py --issue ID --url URL --project <issue's project id> --assignee <issue's assignee email> --sid SID` + mode, for a new and a resumed run alike.
- `--pick [--role ROLE] [RUNS_LOG]`: `--role` sets `only`; `--project` is a usage error (exit 2). `USAGE`, the module docstring and the `take` "not found" log (`… is not a Todo issue assigned to a role account`) follow.
- `--plan`/`--claim` output and FR-11 (`last_move(by_user=True)`) are unchanged.

## G7 — Launcher (`scripts/launch.py`)

- Args: `--issue --url --project --assignee --sid --mode [--k]`.
- `jobs = runnable(cfg, root)`; the role is the one whose `account.lower() == assignee.lower()`; none → `launch.py: 'E' is not a role account` on stderr, exit 2 (as the non-runnable project was). No lookup of `--project` in config; it only fills `Project:` in the tail.
- Prompt, key check, `LINEAR_KEYCHAIN_SERVICE`, repo step and project log (per task name) are unchanged.

## G8 — Promote (`scripts/promote.py`)

- `Promoter.__init__`: `self.runs = runnable(cfg)`, `self.roles = role_ids(gql, self.runs)` and its inverse `{role: user id}`. The next-project existence check goes.
- `Q_HANDOFF` filter: team, state Handoff, `project: { null: false }`, `assignee: { id: { in: [role ids] } }`; nodes also select `assignee { id }`.
- An issue is worked when its role has `cfg["roles"][role]["next"]`.
- `promote`: `required = cfg["roles"][role].get("require_instructions", True)`; `cid = child_id(src.id, nxt, first)`; `issueCreate` input adds `assigneeId` = the next role's user id and uses `projectId` = the source's project id; title `child_title(runs[nxt].task["prefix"], runs[role].task.get("prefix"), src.title)`.
- Dry run: `promote <ID> -> new <next role> issue in <project name>`.
- Bounce, relation, Done, `Promoted to` comment and prune are unchanged; no move sets `assigneeId`.

## G9 — Rules and docs

- `tasks/deep-research.md` step 1: `python3 ../scripts/router.py --pick --role researcher`. Step 4's claim drops "assignee `viewer`" (FR-8: claiming only moves to In Progress; a session started outside the launcher acts as the harness account, so assigning `viewer` would take the issue off the researcher's queue).
- `tasks/*.md` `## Board` first bullet: "The <stage> project" becomes "Issues assigned to your role account, in any project".
- README: the model (project = product, assignee = stage: researcher → pm → engineer), using the board (create in a product project, assign a role account; Handoff creates the next role's issue in the same project), schedule wording ("later role"), `--pick --role`, `pipeline.toml` contents, and a **Cutover** section with PRD step 7 (merge without deploying; create product projects; stop the pipeline; reassign open issues by stage project: 1-Research → researcher, 2-Product Design → pm, 3-Engineering → engineer; `git pull --ff-only`; `router.py --now --dry-run` and check `logs/promote.log`).
- CLAUDE.md: intro, Architecture (router queue by assignee, launcher role by assignee, promote same-project hand-off, child id key; promote now validates `roles/` and `tasks/` too, so a broken file stops hand-offs as well), "Roles, tasks, rules" (every role is runnable; `[roles.<role>]` holds `next`/`require_instructions`), Gotchas (ids: team and states; roles by name).
- `pipeline.toml` comments; `router.py`, `launch.py` and `promote.py` docstrings.

## Testing

`python3 -m unittest discover -s scripts/tests` (no network, Keychain or Claude); existing tests move from project fixtures to role fixtures. New or changed:

- **pipeline:** `[projects]` rejected as unknown; `next` naming an undefined role rejected; `next` role whose default task lacks `prefix` rejected; `[roles.X]` for an undefined role and unknown `[roles.X]` keys rejected; cycle rejected; `stage_order` over roles; `runnable` keyed by role with the default task; `role_ids` maps ids and stops on an unknown account.
- **router:** the issues filter carries team, `project: {null: false}`, role account ids and state; issues assigned to others or unassigned are never claimed; order priority → later role → older; the claim sets only `stateId`; Recover scans role-assigned In Progress issues across projects and its requeue sets no `assigneeId`; `--pick --role` restricts to that role, `--pick --project` exits 2; `launch.py` gets `--assignee` and `--project`.
- **launch:** the role (charter, task, key, log) comes from `--assignee`, case-insensitively; a non-role assignee exits 2; an unknown `--project` still launches and appears as `Project:`.
- **promote:** the child is created in the source's project, assigned to the next role's id, titled with the next role's prefix (source prefix stripped); a Handoff of a role without `next` is left alone; `require_instructions` comes from the source role; the child id uses the next role; no mutation sets `assigneeId` on the source.

Done when: `router.py --now --dry-run` queues only Todo issues assigned to role accounts, across projects; a Handoff creates the child in the same project, assigned to the next role.
