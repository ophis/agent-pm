# TASK-66: reference the Linear team and workflow states by id — plan

RESUME: phase=S5 worktree=/Users/francis/playground/agent-pm/work/TASK-66/worktrees/TASK-66-reference-the-linear-team-and-workflow-s branch=TASK-66-reference-the-linear-team-and-workflow-s base_ref=b5473cec0fe9b27b79d319dfa6ea9ac350197d01 review_round=0 spec_file=/Users/francis/playground/agent-pm/work/TASK-66/worktrees/TASK-66-reference-the-linear-team-and-workflow-s/docs/specs/2026-09-29-task-66-ids-not-names-design.md

## Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** agent-pm keys the Linear team and its workflow states by id, so a rename in Linear breaks nothing.

**Architecture:** `pipeline.toml` gains `team = <id>` and `[states]`; `pipeline.load_config` checks their shape offline and `pipeline.team(gql, cfg)` checks them against Linear in one query, returning a `Team` that router, promote and prune use for every filter and move. The launcher passes the ids to sessions in the prompt tail.

**Tech Stack:** Python 3.11+ stdlib (`tomllib`, `unittest`), Linear GraphQL.

**Spec:** `docs/specs/2026-09-29-task-66-ids-not-names-design.md` (read it with this plan).

Worktree (all paths absolute from here): `W=/Users/francis/playground/agent-pm/work/TASK-66/worktrees/TASK-66-reference-the-linear-team-and-workflow-s`, branch `TASK-66-reference-the-linear-team-and-workflow-s`. Before any write: `git -C $W branch --show-current` must print that branch. After each task commit run exactly `git -C /Users/francis/playground/agent-pm/work/TASK-66/worktrees/TASK-66-reference-the-linear-team-and-workflow-s push -u origin TASK-66-reference-the-linear-team-and-workflow-s`. Never force-push, merge or touch `main`.

## Global Constraints

- Python 3.11+, stdlib only; tests: `python3 -m unittest discover -s $W/scripts/tests` (no network, Keychain or Claude).
- Code style: match the files (compact, few comments; a comment only for a non-obvious reason). Errors from config are `SystemExit` messages starting `pipeline.toml:`.
- Real ids (from the spec): team `06159b6b-5efe-4bc5-a27b-875701f40d61`; todo `717e27f7-c97e-43d8-aa47-13d513ad8ec6`, in_progress `cda9dcfe-c2ab-4853-8e17-8d6ad76b0795`, in_review `5efb3bbc-ea0b-4982-a3d5-31338c9bade3`, handoff `6d876dc6-7732-4473-a0b2-30041d93d0aa`, done `414a4381-7260-46e7-8057-d8cec9d4b11f`, canceled `ed6093ef-3f2e-4e8e-bb59-65ec56ae1938`.
- Logical state keys, in this order everywhere: `todo, in_progress, in_review, handoff, done, canceled`; labels `Todo, In Progress, In Review, Handoff, Done, Canceled`.
- Grep gate at the end: `grep -nE 'name: \{ eq|workflowStates\(|state \{ name \}' $W/scripts/*.py` prints nothing.
- Commit messages: `TASK-66: <what>`, ending with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

- A config value that is not a lowercase UUID (typo, `"Frank's Agents"` left over, an int): a clear `pipeline.toml:` message, never a Linear GraphQL validation error (Task 1 tests).
- A `[states]` id that exists in Linear but belongs to another team: stops at start listing the key (Task 1 `team()` test).
- A state renamed in Linear (same id): router/promote/prune behave unchanged — fakes return no state names, so any name lookup fails the tests (Tasks 2–3).
- promote with a bad `[states]` id: stops before any mutation (Task 3 test).
- A resumed session gets the same `Team:`/`States:` tail as a new one (Task 4 test).

---

### Task 1: pipeline.py ids, `team()`, pipeline.toml, test fixtures

**Files:**
- Modify: `$W/scripts/pipeline.py` (constants near `TOP_KEYS`; `load_config`; new `Team`, `Q_TEAM`, `team()` after `reviewer()`)
- Modify: `$W/pipeline.toml`
- Create: `$W/scripts/tests/board_ids.py`
- Modify: `$W/scripts/tests/test_pipeline.py`, and the `CONFIG` fixtures' first line in `$W/scripts/tests/test_router.py`, `test_promote.py`, `test_launch.py`

**Interfaces:**
- Produces: `pipeline.STATES: dict[str, str]` (key → label, ordered); `pipeline.Team(id: str, name: str, projects: dict[str, str], states: dict[str, str])` frozen dataclass; `pipeline.Q_TEAM: str`; `pipeline.team(gql, cfg) -> Team`. Test helper `board_ids`: `TEAM: str`, `STATES: dict[str, str]` (key → id), `HEADER: str` (TOML: team line + inline `states = {…}` line), `team_node(state_ids=None, projects=()) -> dict` (a `teams.nodes[0]` shaped node).

- [ ] **Step 1: shared test ids.** Create `scripts/tests/board_ids.py`:

```python
"""Linear team and state ids for the tests' pipeline.toml fixtures and fake Linear servers."""
TEAM = "00000000-0000-4000-8000-000000000001"
STATES = {k: f"00000000-0000-4000-8000-0000000000{i:02d}" for i, k in
          enumerate(("todo", "in_progress", "in_review", "handoff", "done", "canceled"), start=11)}
# Inline table: fixtures may append top-level keys or [projects] tables after it.
HEADER = f'team = "{TEAM}"\nstates = {{ {", ".join(f"{k} = \"{v}\"" for k, v in STATES.items())} }}\n'


def team_node(state_ids=None, projects=()):
    """teams.nodes[0] of pipeline.Q_TEAM; projects: (id, name) pairs."""
    return {"id": TEAM, "name": "Team", "states": {"nodes": [{"id": i} for i in (state_ids or STATES.values())]},
            "projects": {"nodes": [{"id": i, "name": n} for i, n in projects]}}
```

(If the nested f-string quoting is rejected by 3.11, build the inline table with a plain `", ".join('%s = "%s"' % kv for kv in STATES.items())`.)

- [ ] **Step 2: fixtures.** In `test_pipeline.py` (`BASE`, `PROJECTS`), `test_router.py`, `test_promote.py`, `test_launch.py`, replace each fixture's leading `team = "T"\n` with `HEADER` (`from board_ids import HEADER`, plus `TEAM`, `STATES`, `team_node` where used later), e.g. `CONFIG = HEADER + """human_members = ["me@x.com"]\n[projects.p-dr]…"""`. The tests dir is on `sys.path` under `unittest discover -s scripts/tests`.

- [ ] **Step 3: failing tests** in `test_pipeline.py` (class `Config` uses `self.load(text)`):

```python
    def test_ids_required(self):
        from board_ids import TEAM, STATES
        states = "states = { " + ", ".join(f'{k} = "{v}"' for k, v in STATES.items()) + " }\n"
        body = BASE[BASE.index("[projects"):]
        cases = {
            "team must be a Linear team id (UUID): \"Frank's Agents\"": 'team = "Frank\'s Agents"\n' + states + body,
            "team must be a Linear team id (UUID): None": states + body,
            "[states] is missing: todo, in_progress": f'team = "{TEAM}"\n' + body,
            "[states] is missing: canceled": f'team = "{TEAM}"\n' + states.replace(f', canceled = "{STATES["canceled"]}"', "") + body,
            "[states] has unknown keys: backlog": f'team = "{TEAM}"\n' + states.replace(" }", ', backlog = "x" }') + body,
            "states.done must be a Linear workflow state id (UUID): 'Done'": f'team = "{TEAM}"\n' + states.replace(STATES["done"], "Done") + body,
        }
        for fragment, text in cases.items():
            with self.subTest(fragment), self.assertRaises(SystemExit) as cm:
                self.load(text)
            self.assertTrue(str(cm.exception.code).startswith("pipeline.toml: "), cm.exception.code)
            self.assertIn(fragment, str(cm.exception.code))
```

Adjust the expected fragments to the exact messages of Step 5 (missing lists every missing key in `STATES` order; the `None` case is `team` absent). Also in class `Runnable`: `test_states_is_a_top_key` — `self.runs()` accepts `HEADER + …` (no "unknown keys: states"). And a real-config test next to `test_load_config_does_not_need_files`:

```python
    def test_real_config_ids(self):
        cfg = pipeline.load_config()
        self.assertEqual(list(cfg["states"]), list(pipeline.STATES))
        self.assertEqual(cfg["team"], "06159b6b-5efe-4bc5-a27b-875701f40d61")
```

New class `TeamCheck` for `team()`:

```python
class TeamCheck(unittest.TestCase):
    def cfg(self):
        from board_ids import TEAM, STATES
        return {"team": TEAM, "states": dict(STATES)}

    def gql(self, nodes):
        calls = []
        def gql(query, **v):
            calls.append((query, v))
            return {"teams": {"nodes": nodes}}
        gql.calls = calls
        return gql

    def test_ok(self):
        from board_ids import TEAM, STATES, team_node
        gql = self.gql([team_node(projects=[("p1", "One")])])
        t = pipeline.team(gql, self.cfg())
        self.assertEqual(t, pipeline.Team(TEAM, "Team", {"p1": "One"}, dict(STATES)))
        (query, v), = gql.calls
        self.assertEqual((query, v), (pipeline.Q_TEAM, {"t": TEAM}))
        self.assertIn("teams(filter: { id: { eq: $t } })", query)

    def test_team_not_found(self):
        from board_ids import TEAM
        with self.assertRaises(SystemExit) as cm:
            pipeline.team(self.gql([]), self.cfg())
        self.assertEqual(str(cm.exception.code), f"pipeline.toml: team {TEAM} not found in Linear")

    def test_states_outside_team(self):
        from board_ids import STATES, team_node
        other = [i for k, i in STATES.items() if k not in ("handoff", "done")]
        with self.assertRaises(SystemExit) as cm:
            pipeline.team(self.gql([team_node(other)]), self.cfg())
        self.assertEqual(str(cm.exception.code), "pipeline.toml: [states] not workflow states of team 'Team': "
                         f"handoff {STATES['handoff']}, done {STATES['done']}")
```

- [ ] **Step 4:** run `python3 -m unittest discover -s $W/scripts/tests -k ids -k Team -k real_config`; expect FAIL (no `STATES`/`team`).

- [ ] **Step 5: implement** in `pipeline.py`. Rename `SID_RE` → `UUID_RE` (only used in `transcript()`), then:

```python
TOP_KEYS = {"team", "states", "human_members", "projects"}
# Logical workflow states the code uses -> the name the docs use (a label; Linear is always queried by id).
STATES = {"todo": "Todo", "in_progress": "In Progress", "in_review": "In Review",
          "handoff": "Handoff", "done": "Done", "canceled": "Canceled"}


def _uuid(v):
    return isinstance(v, str) and bool(UUID_RE.fullmatch(v))


def _check_ids(cfg):
    if not _uuid(cfg.get("team")):
        raise SystemExit(f"pipeline.toml: team must be a Linear team id (UUID): {cfg.get('team')!r}")
    states = cfg.get("states")
    if not isinstance(states, dict):
        states = {}
    if missing := [k for k in STATES if k not in states]:
        raise SystemExit(f"pipeline.toml: [states] is missing: {', '.join(missing)}")
    if extra := sorted(set(states) - set(STATES)):
        raise SystemExit(f"pipeline.toml: [states] has unknown keys: {', '.join(extra)}")
    for k in STATES:
        if not _uuid(states[k]):
            raise SystemExit(f"pipeline.toml: states.{k} must be a Linear workflow state id (UUID): {states[k]!r}")
```

Call `_check_ids(cfg)` in `load_config` right after `tomllib.load`. After `reviewer()`:

```python
@dataclass(frozen=True)
class Team:
    """The pipeline.toml team as checked by team(): {project id: name}, {logical state: state id}."""
    id: str
    name: str
    projects: dict
    states: dict


Q_TEAM = """query($t: ID) { teams(filter: { id: { eq: $t } }) { nodes { id name
  states(first: 100) { nodes { id } } projects(first: 50) { nodes { id name } } } } }"""


def team(gql, cfg):
    """The pipeline.toml team, checked in one query: it exists and holds every [states] id; a bad id stops the caller."""
    nodes = gql(Q_TEAM, t=cfg["team"])["teams"]["nodes"]
    if not nodes:
        raise SystemExit(f"pipeline.toml: team {cfg['team']} not found in Linear")
    t = nodes[0]
    ids = {s["id"] for s in t["states"]["nodes"]}
    if bad := [f"{k} {cfg['states'][k]}" for k in STATES if cfg["states"][k] not in ids]:
        raise SystemExit(f"pipeline.toml: [states] not workflow states of team {t['name']!r}: {', '.join(bad)}")
    return Team(t["id"], t["name"], {p["id"]: p["name"] for p in t["projects"]["nodes"]}, {k: cfg["states"][k] for k in STATES})
```

Update the module docstring's first line only if it lists what pipeline.py holds (it says "Linear access, paths and pipeline.toml" — fine as is).

- [ ] **Step 6: pipeline.toml**: first lines become

```toml
team = "06159b6b-5efe-4bc5-a27b-875701f40d61"   # Frank's Agents; Linear ids, since names change with a rename
human_members = ["ophis.w@outlook.com"]   # (unchanged line and comment)

# The team's workflow states the code uses, by Linear state id.
[states]
todo        = "717e27f7-c97e-43d8-aa47-13d513ad8ec6"
in_progress = "cda9dcfe-c2ab-4853-8e17-8d6ad76b0795"
in_review   = "5efb3bbc-ea0b-4982-a3d5-31338c9bade3"
handoff     = "6d876dc6-7732-4473-a0b2-30041d93d0aa"
done        = "414a4381-7260-46e7-8057-d8cec9d4b11f"
canceled    = "ed6093ef-3f2e-4e8e-bb59-65ec56ae1938"
```

`[states]` must come after `human_members` (a table header ends the top-level keys) and before the `[projects…]` comment block.

- [ ] **Step 7:** full suite `python3 -m unittest discover -s $W/scripts/tests` — all pass (router/promote/prune still use names; their fakes ignore the team value). Commit `TASK-66: team and state ids in pipeline.toml, checked by load_config and team()`, push.

### Task 2: router by id

**Files:** Modify `$W/scripts/router.py` (`Board`, lines ~158–291; import line), `$W/scripts/tests/test_router.py`.

**Interfaces:** Consumes `pipeline.team(gql, cfg) -> Team`, `Team.states` / `Team.projects`. `Board.issues(state, extra)`, `last_move(issue, state, by_user)`, `comment_and_move(issue, body, state, **extra)` now take logical keys (`"todo"`, `"in_progress"`, `"in_review"`).

- [ ] **Step 1: fake to ids.** In `test_router.py`: `from board_ids import HEADER, STATES as IDS_BY_KEY, team_node`; keep test-side names: `STATES = {"Todo": IDS_BY_KEY["todo"], "In Progress": IDS_BY_KEY["in_progress"], "In Review": IDS_BY_KEY["in_review"]}` and `NAMES = {i: n for n, i in STATES.items()}`. Replace every `"s-prog"`, `"s-todo"`, `"s-review"` literal in the file with `STATES[...]` (e.g. `moved(..., state=STATES["In Progress"], …)`). `FakeLinear.__call__`: record `self.queries.append((query, v))` first; then

```python
        if "viewer" in query:
            return {"viewer": {"id": ME}}
        if query == pipeline.Q_TEAM:
            return {"teams": {"nodes": [team_node(self.state_ids, [(IDS[p], p) for p in PROJECTS])]}}
```

(`self.state_ids = None` in `__init__`; a test may set it.) The mutation branch maps `NAMES[upd["stateId"]]`; the `issues(filter` branch matches `i["state"] == NAMES[f["state"]["id"]["eq"]]` and returns nodes without a `state` key; the claim re-check branch is `if "state { id }" in query: return {"issue": {"state": {"id": STATES[self.issues[v["i"]]["state"]]}}}`. Existing tests keep asserting names via `fake.issues[...]["state"]`.

- [ ] **Step 2: new failing tests:**

```python
    def test_queries_by_id(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME, updated=ago(hours=5)), issue("TASK-2", "Todo"))
        self.run_router(fake, "--claim")   # use this file's helper that calls router.main(... config=self.config)
        flts = [v["f"] for q, v in fake.queries if "issues(filter" in q]
        self.assertTrue(flts)
        self.assertTrue(all(set(f["state"]) == {"id"} and f["state"]["id"]["eq"] in STATES.values() for f in flts))
        text = " ".join(q for q, _ in fake.queries)
        self.assertNotIn("name: { eq", text)
        self.assertNotIn("workflowStates", text)
        self.assertNotIn("state { name }", text)
        self.assertEqual(fake.issues["TASK-2"]["state"], "In Progress")   # claimed by id

    def test_state_outside_team_stops_before_changes(self):
        fake = self.fake(issue("TASK-1", "Todo"))
        fake.state_ids = [i for i in IDS_BY_KEY.values() if i != IDS_BY_KEY["in_review"]]
        with self.assertRaises(SystemExit) as cm:
            self.run_router(fake, "--claim")
        self.assertIn("[states] not workflow states of team", str(cm.exception.code))
        self.assertEqual(fake.mutations, [])
```

Use the file's existing way of running `--claim`/`--plan` (see `router.main(list(argv) + [self.log], gql=fake, …, config=self.config)` around line 146) instead of the placeholder name `run_router`.

- [ ] **Step 3:** run `-k test_router`; expect the new tests (and fake-dependent ones) to FAIL.

- [ ] **Step 4: implement** in `router.py`: import `team` from `pipeline`. `Board.__init__`:

```python
        self.me = gql("query { viewer { id } }")["viewer"]["id"]
        t = team(gql, cfg)
        self.states = t.states
        self.reviewer = reviewer(gql, cfg)
        if not self.projects:
            raise SystemExit(...)   # unchanged
        missing = [p for p in self.projects if p not in t.projects]
        if missing:
            raise SystemExit(f"runnable projects not found in Linear: {', '.join(missing)}")
```

`issues`: `flt = {"project": {"id": {"in": self.projects}}, "state": {"id": {"eq": self.states[state]}}, **(extra or {})}` and drop `state { name }` from its nodes. Every call site uses keys: `self.last_move(issue, "todo", by_user=True)`, `self.last_move(issue, "in_progress")`, `self.issues("in_progress", …)`, `self.issues("todo")`, `comment_and_move(issue, CAP_COMMENT, "in_review")`, `comment_and_move(issue, INTERRUPTED, "todo", assigneeId=None)`; in `comment_and_move`: `if state == "in_review" and self.reviewer:`. Claim re-check:

```python
            current = self.gql("query($i: String!) { issue(id: $i) { state { id } } }", i=issue["id"])["issue"]["state"]["id"]
            if current != self.states["todo"]:
                log(f"claim: {issue['identifier']} is no longer Todo; skipping")
                return None
            ... s=self.states["in_progress"] ...
```

Update any test asserting the old `claim: … is now <name>` log line.

- [ ] **Step 5:** full suite passes; `grep -nE 'name: \{ eq|workflowStates\(|state \{ name \}' $W/scripts/router.py` prints nothing. Commit `TASK-66: router filters and moves by state id`, push.

### Task 3: promote and prune by id; promote hands its Team to prune

**Files:** Modify `$W/scripts/promote.py`, `$W/scripts/prune.py`, `$W/scripts/tests/test_promote.py`, `$W/scripts/tests/test_prune.py`.

**Interfaces:** Consumes `pipeline.team`, `pipeline.Team`, `pipeline.Q_TEAM`. Produces `prune.Pruner(gql, cfg, now, dry, run=sh_run, work=WORK, team=None)`; `promote.run_prune(gql, cfg, now, dry, pruner=None, team=None)` calls `pruner(gql, cfg, now, dry, team=team)`; `Promoter.team: Team`.

- [ ] **Step 1: test_promote fake.** `from board_ids import HEADER, STATES as IDS_BY_KEY, TEAM, team_node`; `import pipeline`. `STATES = {"Todo": IDS_BY_KEY["todo"], "In Progress": IDS_BY_KEY["in_progress"], "In Review": IDS_BY_KEY["in_review"], "Handoff": IDS_BY_KEY["handoff"], "Done": IDS_BY_KEY["done"]}`; `moved(..., frm=None)` defaults to `STATES["In Progress"]`. Fake: `if query == pipeline.Q_TEAM: return {"teams": {"nodes": [team_node(self.state_ids, [(i, n) for n, i in PROJECTS.items()])]}}` (`self.state_ids = None`); `Q_HANDOFF`: record `self.handoff_vars = v`, return issues with `i["state"] == "Handoff"`; `Q_DETAIL` returns `"state": {"id": STATES[i["state"]]}`. `FakePruner.__init__(self, gql, cfg, now, dry, team=None)` records `(gql, cfg, now, dry, team)`; update the test at ~line 535 to unpack five and assert `team == pipeline.Team(TEAM, "Team", {i: n for n, i in PROJECTS.items()}, dict(IDS_BY_KEY))`. Replace `"s-todo"`/`"team"` in the child assertion (~line 138) with `STATES["Todo"]`/`TEAM`.

- [ ] **Step 2: failing promote tests:**

```python
    def test_handoff_query_by_id(self):
        self.ready()
        self.run_main()
        self.assertEqual(self.fake.handoff_vars, {"t": TEAM, "s": IDS_BY_KEY["handoff"]})
        self.assertIn("state: { id: { eq: $s } }", promote.Q_HANDOFF)
        self.assertIn("team: { id: { eq: $t } }", promote.Q_HANDOFF)

    def test_bad_state_id_stops_before_changes(self):
        self.ready()
        self.fake.state_ids = [i for k, i in IDS_BY_KEY.items() if k != "done"]
        with self.assertRaises(SystemExit) as cm:
            self.run_main()
        self.assertIn("[states] not workflow states of team 'Team': done", str(cm.exception.code))
        self.assertEqual(self.fake.mutations, [])
```

- [ ] **Step 3: test_prune.** `from board_ids import STATES as IDS_BY_KEY, TEAM, team_node`; `import pipeline`. `STATE_IDS = {"Done": IDS_BY_KEY["done"], "Canceled": IDS_BY_KEY["canceled"], "In Progress": IDS_BY_KEY["in_progress"]}`. `gql_for`: `if query == pipeline.Q_TEAM: return {"teams": {"nodes": [team_node()]}}`; issue node `"state": {"id": STATE_IDS[state]}`. `prune()` helper: cfg `{"team": TEAM, "states": dict(IDS_BY_KEY)}`, optional `team=None` passed through to `Pruner`. `tick()`: route `prune.Q_ISSUE` to `prune_gql`, everything else to `linear`. `test_prune_failure_leaves_handoff_alone`: the failure now surfaces per issue — assert `"prune-error TASK-49: Linear: linear api error: down" in out`. New failing tests:

```python
    def test_given_team_skips_team_query(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        given = pipeline.Team(TEAM, "Team", {}, dict(IDS_BY_KEY))
        self.assertEqual(self.prune(gql, team=given)[0], 0)
        self.assertNotIn(pipeline.Q_TEAM, gql.calls)
        self.assertEqual(self.git.calls, self.removed(wt, "TASK-49-x"))

    def test_resolves_team_itself(self):
        self.mkw("TASK-49", "TASK-49-x")
        gql = gql_for({"TASK-49": ("Canceled", [(30, "Canceled")])})
        self.prune(gql)
        self.assertEqual(gql.calls[0], pipeline.Q_TEAM)
        self.assertIn("state { id }", prune.Q_ISSUE)
```

Keep the existing "no Linear query without worktrees" test passing.

- [ ] **Step 4:** run `-k test_promote -k test_prune`; expect FAIL.

- [ ] **Step 5: implement promote.py.** Import `team` from `pipeline`; delete `STATES` and `Q_SETUP`.

```python
Q_HANDOFF = """query($t: ID, $s: ID) { issues(filter: { team: { id: { eq: $t } }, state: { id: { eq: $s } } }, first: 100) {
  nodes { id identifier url title priority createdAt project { id name } attachments { nodes { title url } } } } }"""
```

`Q_DETAIL`: `state { id }`. `Promoter.__init__`:

```python
        self.team = team(gql, cfg)
        self.states, self.projects = self.team.states, self.team.projects
        self.reviewer = reviewer(gql, cfg)
        self.humans = ...   # unchanged
        missing = [p["next"] for p in cfg.get("projects", {}).values() if p.get("next") and p["next"] not in self.projects]
        if missing:
            raise SystemExit(f"not found in Linear: {', '.join(missing)}")
```

`run`: `self.gql(Q_HANDOFF, t=self.team.id, s=self.states["handoff"])`; `if detail["state"]["id"] != self.states["handoff"]:`. `moves`: `self.states["in_review"]`, `self.states["handoff"]`. `promote`: `comment_and_move(src, NO_INSTRUCTIONS, "in_review")`, `"teamId": self.team.id`, `"stateId": self.states["todo"]`, `self.move(src, "done")`. `bounce_failed`: `"in_review"`. `move`: `if state == "in_review" and self.reviewer:`. `main`:

```python
    promoter = Promoter(gql, cfg, now, dry, wait="--now" not in argv)
    promoter.run()
    run_prune(gql, cfg, now, dry, pruner, promoter.team)
```

`run_prune(gql, cfg, now, dry, pruner=None, team=None)`: `pruner(gql, cfg, now, dry, team=team).run()`.

- [ ] **Step 6: implement prune.py.** `from pipeline import WORK, linear_gql, load_config, parse_time, team as linear_team`; delete `FINISHED` and `Q_SETUP`; `Q_ISSUE` selects `state { id }`. `Pruner.__init__(self, gql, cfg, now, dry, run=sh_run, work=WORK, team=None)` stores `self.team = team`. In `run`, replace the setup lines with

```python
        states = (self.team or linear_team(self.gql, self.cfg)).states
        finished = {states["done"], states["canceled"]}
```

and the check with `if not issue or issue["state"]["id"] not in finished:`. Module docstring: unchanged wording is fine ("Done or Canceled" are labels).

- [ ] **Step 7:** full suite passes; grep gate on `promote.py prune.py` prints nothing. Commit `TASK-66: promote and prune by team and state id`, push.

### Task 4: launcher prompt tail, principles and docs

**Files:** Modify `$W/scripts/launch.py` (`main`, tail line ~133; import), `$W/scripts/tests/test_launch.py`, `$W/roles/principles.md` (line 3), `$W/README.md` (launcher bullet, `pipeline.toml` bullet), `$W/CLAUDE.md` (Architecture registry line; Gotchas "the team is still matched by name").

**Interfaces:** Consumes `pipeline.STATES`, `cfg["team"]`, `cfg["states"]` (shape already checked by `load_config`).

- [ ] **Step 1: failing tests** in `test_launch.py`: `from board_ids import HEADER, STATES as IDS_BY_KEY, TEAM`; `IDS = f" Team: {TEAM}. States: " + ", ".join(f"{pipeline.STATES[k]}={IDS_BY_KEY[k]}" for k in pipeline.STATES) + "."`. Every expected tail gains `IDS` right after `Project: <id>.`: line ~140 `"… Project: p-dr." + IDS`; ~168 and ~173 `endswith(… "Project: p-dr." + IDS)`; ~183 likewise; ~220 `" Project: p-eng." + IDS + " Repo check: OK …"`; ~235 `endswith(f" Project: p-eng.{IDS} Repo check failed: …")`. The real-config helper `expected()` (~435) builds its ids from `pipeline.load_config()`: `cfg = pipeline.load_config()`; `real = f" Team: {cfg['team']}. States: " + ", ".join(f"{pipeline.STATES[k]}={cfg['states'][k]}" for k in pipeline.STATES) + "."` inserted after `Project: {project}.`. Add one explicit check that the resume prompt ends with the same `IDS` tail as the new one.

- [ ] **Step 2:** run `-k test_launch`; expect FAIL.

- [ ] **Step 3: implement** in `launch.py`, import `STATES` from `pipeline`, and:

```python
    tail = (f" Reviewer: {(humans or ['none'])[0]}. Humans: {', '.join(humans) or 'none'}. Project: {a.project}."
            f" Team: {cfg['team']}. States: {', '.join(f'{STATES[k]}={cfg['states'][k]}' for k in STATES)}.")
```

(Nested same-quote f-strings need 3.12; on 3.11 precompute `states = ", ".join(f"{STATES[k]}={cfg['states'][k]}" for k in STATES)`.)

- [ ] **Step 4: principles.md**, replace the first bullet `- Team \`Frank's Agents\`; the run's project id is the prompt's \`Project:\` value (the name may change).` with:

```markdown
- Team `Frank's Agents`; the run's project id is the prompt's `Project:` value. Names (the team, projects, and statuses such as Todo or In Review in these files) are for reading and may change in Linear: act by id — the team's is the prompt's `Team:` value, each status's is in its `States:` line (`<name>=<id>`); move and filter issues by those ids, never look one up by name.
```

- [ ] **Step 5: README.md** — launcher bullet: the prompt line becomes `Reviewer: <email>. Humans: <emails>. Project: <id>. Team: <id>. States: <name>=<id>, ….` (sessions move issues by those ids; tasks name statuses for readability). `pipeline.toml` bullet: starts `team (the Linear team id), [states] (the team's workflow state ids for todo, in_progress, in_review, handoff, done, canceled; router, promote and prune check them against Linear at start and stop on a bad id), human_members …`; keep "names and URL slugs change on rename" and add that this holds for the team and states too.

- [ ] **Step 6: CLAUDE.md** — registry sentence: "`pipeline.toml` with only `team` (id), `[states]` (ids), `human_members` and `[projects."<id>"]` …". Gotcha: replace "`pipeline.toml` keys projects (and `next`) by Linear project id, so renaming a project needs no change; the team is still matched by name." with "`pipeline.toml` keys the team, its workflow states (`[states]`) and projects (and `next`) by Linear id, so a rename needs no change; a new or replaced state needs its id in `[states]`. `load_config` checks their shape, `pipeline.team()` checks them against Linear at the start of router, promote and prune. Sessions get the ids in the prompt tail (`Team:`, `States:`)."

- [ ] **Step 7:** full suite passes; grep gate on `$W/scripts/*.py` prints nothing. Commit `TASK-66: sessions get team and state ids; docs`, push.

## Verification (S6)

- `python3 -m unittest discover -s $W/scripts/tests` — all pass.
- Grep gate prints nothing.
- Read-only live check: `cd $W && python3 scripts/promote.py --dry-run` and `python3 scripts/router.py --plan --dry-run` (Linear reads only; `--plan --dry-run` makes no changes) — no config error.

## Progress

- decision(path): architectural (config interface across router/promote/prune/launcher); spec in docs/specs per CLAUDE.md; solo.
- decision(validation site): offline shape check in load_config + one `team()` Linear query in router/promote/prune, launcher only passes ids on - router validated the same file this tick; dissent: none.
- decision(team in prompt): tail also passes `Team: <id>` so principles.md's team line is rename-proof like the states; solo.
- S3 panel: core=[architecture,spec-fitness] +optional=[] (security dropped: config ids only, no untrusted input) transport=Workflow
- S3 r0: architecture=PASS spec-fitness=PASS -> converged. Folded non-blockers: eng.py not a load_config caller; Pruner takes promote's Team (no duplicate query); launcher-only cfg["states"] read; test fixtures get the new keys.
