# TASK-67: role identity — per-role config and Linear keys — design

Source: TASK-67 (https://linear.app/ophis-workgroup/issue/TASK-67), PR-A of the TASK-62 PRD (`private_docs/Product Design/2026-09-29-1540-TASK-62-board-model-products-roles-tasks.md`, section 实施顺序 step 2). Each agent session acts in Linear as its role's own account; dispatch stays by project; router, promote and prune keep acting as the harness account `frank.agent.w@gmail.com`.

## Goals

1. **G1 — Role config (FR-1).** `roles/<role>.toml` gains `tasks`, `account`, `key`, validated at load.
2. **G2 — Task prefix (FR-2).** `tasks/<task>.toml` gains optional `prefix`.
3. **G3 — Harness key (FR-14).** `pipeline.toml` gains `harness_key`; the harness reads its Linear key from that Keychain service, by service only.
4. **G4 — Session key (FR-13).** The launcher checks the role's Keychain item exists and sets `LINEAR_KEYCHAIN_SERVICE=<role key>` for the session; it never reads the key.
5. **G5 — Runs as before.** Moves made by a role account never count as the user's in the router's attempt cap.
6. **G6 — Rules and docs (FR-22, FR-23).** `roles/principles.md` names the session's identity; README covers role-account onboarding; CLAUDE.md the architecture.
7. **G7 — Security and tests.** No key in env vars, argv, the tmux environment, logs or prompts; unit tests cover every new or changed behavior.

## Non-goals

- FR-3–FR-12, FR-16–FR-18 (PR-B, PR-C): `[projects]` keeps `role`, `task`, `prefix`, `next`; promote keeps using the project's `prefix`; the task `prefix` is loaded and validated only.
- FR-15: the launch prompt already passes the resolved task file (`Run.instructions`); no change. The `Reviewer:` argument stays (removed with FR-17 in PR-B).
- FR-24: the `linear` skill (`~/.claude/skills/linear`) already honours `LINEAR_KEYCHAIN_SERVICE`; it is outside this repo.
- `eng.py`'s CLI (run inside Engineering sessions) keeps the harness key through `linear_gql`; it only reads Linear.

## G1 — `roles/<role>.toml`

```toml
# roles/researcher.toml
tasks = ["deep-research"]                         # tasks this role can run; the first is its default
account = "frank.agent.w+researcher@gmail.com"    # the role's Linear account
key = "linear-api-key-researcher"                 # Keychain service holding that account's API key
```

| role | tasks | account | key |
| -- | -- | -- | -- |
| researcher | `["deep-research"]` | `frank.agent.w+researcher@gmail.com` | `linear-api-key-researcher` |
| pm | `["product-design"]` | `frank.agent.w+pm@gmail.com` | `linear-api-key-pm` |
| engineer | `["engineering"]` | `frank.agent.w+engineer@gmail.com` | `linear-api-key-engineer` |

`read_only` and `memory` are unchanged. `ROLE_KEYS` gains `tasks`, `account`, `key`, all three required.

Validation (a violation stops router and launcher with a `SystemExit` naming the file):

- per role (`_role`): `tasks` is a non-empty list of strings; `account` and `key` are non-empty strings;
- across the registry (`registry(root, harness_key)`): every `tasks` entry names a `tasks/<task>.md` + `.toml` pair; no two roles share a `key`; no role's `key` equals `harness_key`;
- per runnable project (`runnable`): the project's `task` is in its role's `tasks` (the role's `tasks` are what it can run).

`registry` returns roles as a frozen `Role(read_only, memory, tasks, account, key)` dataclass instead of the `(read_only, memory)` tuple. `runnable(cfg, root)` passes `cfg["harness_key"]` to `registry`. `Run` gains `key` and `account` (its role's).

## G2 — `tasks/<task>.toml`

`TASK_KEYS` gains `prefix`: optional; when present a non-empty string. `tasks/product-design.toml` gets `prefix = "PRD"`, `tasks/engineering.toml` `prefix = "ENG"`; `deep-research` has none.

## G3 — harness key

- `pipeline.toml`: top-level `harness_key = "linear-api-key"` (Keychain service of the harness account's key). `TOP_KEYS` gains it; `load_config` requires a non-empty string.
- `pipeline.linear_gql` reads the service from `pipeline.toml` (`load_config()["harness_key"]`) on each call and runs `security find-generic-password -s <harness_key> -w` — no `-a`. Callers (router, promote, prune, launch's repo step, eng.py) are unchanged.

## G4 — launcher

In `launch.main`, for both `new` and `resume`, after the transcript check and before the repo step:

1. `keychain(job.key)` — default `has_key(service)`: `subprocess.run(["security", "find-generic-password", "-s", service], stdout=DEVNULL, stderr=DEVNULL).returncode == 0` (no `-w`, output discarded; the key is never read). `main` takes it as an injectable `keychain=` parameter like `sh=`.
2. Missing → `fail(plog, issue, "config-error", f"Keychain item {job.key} for role key not found", 2)`: one line in the project log and stderr, no tmux session, exit 2 — the existing config-error path.
3. Present → the tmux script exports `LINEAR_KEYCHAIN_SERVICE=<job.key>` alongside `PATH` and `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` (inside the `bash -c` command, as those are, so a running tmux server's environment does not override it).

The prompt text is unchanged.

## G5 — attempt cap (router)

`Board.last_move(..., by_user=True)` treats a move as the user's when its actor is neither the harness (`viewer`) nor a role account. Before PR-A every agent move was the harness's; now a role session's moves (e.g. deep-research step 6 moving a failed run back to Todo) are made by its role account and would otherwise reset the cap forever.

- `Board.__init__` collects the accounts of the runnable projects' roles and resolves them in one query: `users(filter: { email: { in: [...] } }) { nodes { id email } }`. `self.agents = {viewer id} ∪ those ids`.
- An account not found in Linear stops the router: `SystemExit("role accounts not found in Linear: <emails>")` (as `reviewer()` does for `human_members`).
- `by_user` becomes `n["actorId"] and n["actorId"] not in self.agents`. Claiming and Recover still use the harness `viewer` id.

## G6 — rules and docs

`roles/principles.md`:

- "Linear access" bullet → "Linear access: the `linear` skill. The launcher points it at your role's key, so `viewer` is your role account."
- "Comments by …" bullet → the user is only an email in the prompt's `Humans:` list (`human_members`); role accounts (yours and the other roles') and the harness account `frank.agent.w@gmail.com` (router, promote, prune) are the agent, never the user.
- The Handoff-created issue bullet: "written by the agent account" → "written by the harness account".

`README.md`:

- Setup: the harness key stored under service `harness_key` (`linear-api-key`).
- New "Role accounts" steps, per role in `roles/*.toml`: invite its `account` (a Gmail plus-alias of the harness account) in Linear → Settings → Members; accept in a private window with **Continue with email** (not Google, which signs in as the harness account); generate a personal API key in that account's settings; `security add-generic-password -s <key> -a <account> -w`. A missing item stops that role's runs (`config-error` in its project log).
- Configuration: the new role, task and `pipeline.toml` keys.

`CLAUDE.md` Architecture: `pipeline.py` reads the harness key from service `harness_key`; `launch.py` checks the role's Keychain item and sets `LINEAR_KEYCHAIN_SERVICE`, never reading a key; sessions act as their role account, router/promote/prune as the harness; the role `.toml` keys.

## G7 — security and tests

Keys exist only in the `linear` skill's and harness processes' memory. The launcher's only Keychain call has no `-w`; the session gets a service name.

Tests (`scripts/tests`, no network, Keychain or Claude; every `security` call faked):

- `test_pipeline`: each G1 rejection (empty/non-list `tasks`, unknown task, missing `account`, missing `key`, `key` == `harness_key`, shared `key`, project task outside its role's `tasks`); `Role`/`Run` carry `tasks`, `account`, `key`; `prefix` accepted, empty or non-string rejected; `harness_key` required; `linear_gql` calls `security find-generic-password -s <harness_key> -w` with no `-a` (faked `subprocess.run` and `urlopen`, temp `pipeline.toml`).
- `test_launch`: `LINEAR_KEYCHAIN_SERVICE=<key>` is exported; the existence check is `security find-generic-password -s <key>` with no `-w`; a missing item gives exit 2, a `config-error` project-log line and no tmux call, for `new` and `resume`; with `subprocess.run` faked to return a sentinel secret for any `-w` call, the sentinel appears nowhere in the tmux argv, the exported environment or the project log, and no `-w` call is made.
- `test_router`: a Todo move by a role account does not reset the cap; one by another member still does; an unknown role account stops the router.
- Existing fixtures gain the new required keys (`harness_key` in `board_ids.HEADER`; `tasks`, `account`, `key` in role fixtures).
