# TASK-66: reference the Linear team and workflow states by id — design

Source: TASK-66 (https://linear.app/ophis-workgroup/issue/TASK-66). Renaming the team or a workflow state in Linear must not break router, promote, prune or the agent sessions. Projects are already keyed by id; this does the same for the team and the states.

## Goals

1. **G1 — Config.** `pipeline.toml` holds the team id and a `[states]` table of the six logical states the code uses.
2. **G2 — Code by id.** router, promote and prune filter and move by these ids; no code path looks a team or state up by name or compares a state by name.
3. **G3 — Validation.** Offline shape check in `load_config`; one Linear check at start (team exists, every state id belongs to it); a bad id stops with a clear message.
4. **G4 — Sessions by id.** The launcher's prompt tail passes the team and state ids; `roles/principles.md` says to act by those ids; tasks keep state names.
5. **G5 — Tests and docs.** Tests cover the id-based queries and the validation; README and CLAUDE.md document the new keys.

## Non-goals

- `human_members` stays keyed by email; `reviewer()` is unchanged.
- No change to task labels (TASK-64) or the board model (TASK-62). The shared `team()` helper (G3) is the single place TASK-62's team-wide queries can build on.
- `tasks/*.md` keep their state names; no task step changes.
- Log lines and user-facing messages may keep state names (they are labels, not lookups).

## G1 — `pipeline.toml`

```toml
team = "06159b6b-5efe-4bc5-a27b-875701f40d61"   # Frank's Agents (Linear team id; a name changes with a rename)
human_members = [...]                            # unchanged

[states]   # Linear workflow state ids of the team, by the logical state the code uses
todo        = "717e27f7-c97e-43d8-aa47-13d513ad8ec6"
in_progress = "cda9dcfe-c2ab-4853-8e17-8d6ad76b0795"
in_review   = "5efb3bbc-ea0b-4982-a3d5-31338c9bade3"
handoff     = "6d876dc6-7732-4473-a0b2-30041d93d0aa"
done        = "414a4381-7260-46e7-8057-d8cec9d4b11f"
canceled    = "ed6093ef-3f2e-4e8e-bb59-65ec56ae1938"
```

(Ids re-checked against Linear on 2026-09-29: `teams(filter: { id })` returns team "Frank's Agents" with exactly these six states plus Backlog and Duplicate.)

## G2 — `scripts/pipeline.py`

- `STATES = {"todo": "Todo", "in_progress": "In Progress", "in_review": "In Review", "handoff": "Handoff", "done": "Done", "canceled": "Canceled"}` — logical key → the name the docs use (a label; never sent to Linear as a filter).
- `TOP_KEYS` gains `"states"`.
- `load_config` (offline, every consumer) additionally checks, each failure a `SystemExit` starting `pipeline.toml:`:
  - `team` is a string UUID (`team must be a Linear team id (UUID)`); a non-UUID would otherwise reach Linear's `id` comparator, which rejects it with a GraphQL validation error instead of a clear message.
  - `states` is a table with exactly the `STATES` keys: missing ones listed (`[states] is missing: …`), unknown ones listed (`[states] has unknown keys: …`), and every value a string UUID (`states.<key> must be a Linear workflow state id (UUID)`).
- New `team(gql, cfg)` → `Team` (frozen dataclass: `id`, `name`, `projects` = `{project id: name}`, `states` = `{logical key: state id}`), one query:

  ```graphql
  query($t: ID) { teams(filter: { id: { eq: $t } }) { nodes { id name
    states(first: 100) { nodes { id } } projects(first: 50) { nodes { id name } } } } }
  ```

  No node → `SystemExit("pipeline.toml: team <id> not found in Linear")`. Any configured state id not among the team's states → `SystemExit("pipeline.toml: [states] not workflow states of team <name>: <key> <id>, …")` listing every bad key. Otherwise returns the `Team` with `states = dict(cfg["states"])`.

## G2 — Consumers

**router.py** (`Board`):
- `__init__`: `viewer { id }` in its own query; `t = team(gql, cfg)`; `self.states = t.states`; runnable projects are checked against `t.projects` (same message as today). `reviewer()` stays.
- `issues(state, extra)` takes a logical key; filter `"state": {"id": {"eq": self.states[state]}}`. The unused `state { name }` is dropped from the returned nodes.
- `last_move`, `attempts`, `current_sid`, `comment_and_move`, `recover`, `next_run`, `take`: logical keys (`"todo"`, `"in_progress"`, `"in_review"`) everywhere a state name was used; `comment_and_move` assigns the reviewer when the key is `"in_review"`.
- `take` claim re-check: query `state { id }`, skip unless it equals `self.states["todo"]`; its log line names the state by id.

**promote.py** (`Promoter`):
- `__init__`: `t = team(gql, cfg)`; `self.team = t.id`, `self.states = t.states`, `self.projects = t.projects`. `Q_SETUP` and the `STATES` name tuple go (G3 validates all states); the `next`-project check against `self.projects` stays.
- `Q_HANDOFF`: `query($t: ID, $s: ID) { issues(filter: { team: { id: { eq: $t } }, state: { id: { eq: $s } } }, first: 100) …`, called with `t=self.team, s=self.states["handoff"]`.
- `Q_DETAIL` selects `state { id }`; the lag re-check compares it with `self.states["handoff"]`.
- `moves`, `promote` (child `stateId`), `bounce_failed`, `move`, `comment_and_move`: logical keys.

**prune.py** (`Pruner.run`):
- `Pruner(gql, cfg, now, dry, run, work, team=None)`: promote's `run_prune` passes the `Promoter`'s `Team` (no second `team()` query per promote tick); standalone `prune.py` resolves it itself.
- After the no-worktree early return (Linear is still only queried when a worktree exists): `states = (self.team or team(self.gql, self.cfg)).states`; `finished = {states["done"], states["canceled"]}`. `Q_SETUP` and `FINISHED` go.
- `Q_ISSUE` selects `state { id }`; the finished check is `issue["state"]["id"] in finished`.

Grep gate: no `name: { eq` on a team or state, no `workflowStates(`, no `state { name }` left in `scripts/*.py`.

## G3 — Where validation runs

- `load_config` shape check: router, launcher, promote, prune (every `load_config` caller).
- `team()` Linear check: router `Board.__init__`, `Promoter.__init__`, `Pruner.run` (only when it would query Linear). A bad id stops that tick with the message; promote's `run_prune` already logs a prune `SystemExit` as `prune-error`.
- Consumers holding a `Team` read states from it; only the launcher reads `cfg["states"]` directly, and it does not query Linear for this: the router validated the same `pipeline.toml` seconds before in the same tick, and the launcher only passes the ids on.
- prune's `main` treats only `linear api error` SystemExits as transient (exit 3); a `pipeline.toml:` SystemExit propagates (loud), as today for other config errors.

## G4 — Sessions

- `launch.py` tail, after `Project: <id>.`: ` Team: <team id>. States: Todo=<id>, In Progress=<id>, In Review=<id>, Handoff=<id>, Done=<id>, Canceled=<id>.` — pairs in `STATES` order, labels from `STATES`, so the doc names map to ids. Same tail for new and resumed runs.
- `roles/principles.md`: the `Team \`Frank's Agents\`` bullet becomes: the team and statuses are named in these files for readability; act on them by id — the team's is the prompt's `Team:` value, each status's is in the prompt's `States:` line (`<name>=<id>`); move and filter by those ids, never look a team or status up by name. The project line (`Project:`) stays.

## G5 — Tests and docs

Tests (no network; existing fakes and every test `pipeline.toml` fixture updated to the new queries and keys, incl. the minimal fixtures in `test_pipeline.py`):
- `test_pipeline.py`: shape errors (team missing / non-UUID; `states` missing, missing key, unknown key, non-UUID value) with their messages; the real `pipeline.toml` loads and has all six keys; `team()` returns `Team` on success, stops on no team, stops listing every state id outside the team, and sends the configured team id as `$t`.
- `test_router.py`: fake Linear serves `teams(filter: { id })` and `issues` filtered by `state.id`; a test asserts the issue queries filter `state: { id: { eq: <configured id> } }` and that no query filters by name; claim re-check by id; existing behavior tests pass with ids.
- `test_promote.py`: Handoff query sent with `t` = team id and `s` = handoff id; child `stateId` = todo id and `teamId` = team id; bad config (state not in team) stops before any mutation.
- `test_prune.py`: finished by `state.id` (Done id, Canceled id) with renamed state names in the fake still pruning; no Linear query without worktrees (unchanged); a passed `Team` means no `team()` query; `run_prune` passes the Promoter's `Team`.
- `test_launch.py`: exact tail with `Team:` and `States:` for new and resume.

Docs:
- README: `pipeline.toml` bullet — `team` (Linear team id), `[states]` (the six keys; state ids of that team, validated against Linear at the start of router, promote and prune; a bad id stops with a message), `human_members`, projects; launcher bullet — prompt line adds `Team: <id>. States: <name>=<id>, ….`; the Linear team is still named "Frank's Agents" in prose.
- CLAUDE.md: registry line lists `team` (id), `human_members`, `[states]` and projects; Gotcha "the team is still matched by name" → team and states are keyed by id too (a rename needs no change; a new or replaced state needs its id in `[states]`); agents get the ids in the prompt tail.
- `roles/principles.md` per G4.

## Acceptance

- `python3 -m unittest discover -s scripts/tests` passes.
- Grep gate (G2) holds.
- `python3 scripts/router.py --plan --dry-run` and `python3 scripts/promote.py --dry-run` run against Linear without a config error (manual; reads only).
