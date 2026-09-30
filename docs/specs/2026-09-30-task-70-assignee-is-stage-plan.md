# TASK-70: the assignee is the stage — plan

RESUME: phase=S9 worktree=/Users/francis/playground/agent-pm/work/TASK-70/worktrees/TASK-70-the-assignee-is-the-stage-claim-recover branch=TASK-70-the-assignee-is-the-stage-claim-recover base_ref=be21167bcace11ed397c373c30f40da188817c21 review_round=1 spec_file=docs/specs/2026-09-30-task-70-assignee-is-stage-design.md

## Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dispatch by assignee: router, Recover, launcher and promote find a role's issues by its Linear account in any project; `pipeline.toml` keys the pipeline by role.

**Architecture:** `pipeline.py` loads `[roles.<role>]` (`next`, `require_instructions`), returns `runnable()` keyed by role (default task), and resolves role accounts to Linear user ids (`role_ids`). Router and promote filter issues by team + in a project + assignee id; the launcher maps `--assignee` to the role. No harness mutation sets `assigneeId`.

**Tech Stack:** Python 3.11 stdlib, `unittest` (`python3 -m unittest discover -s scripts/tests`; no network, Keychain or Claude).

**Spec:** `docs/specs/2026-09-30-task-70-assignee-is-stage-design.md`

## Global Constraints

- Worktree `/Users/francis/playground/agent-pm/work/TASK-70/worktrees/TASK-70-the-assignee-is-the-stage-claim-recover`, branch `TASK-70-the-assignee-is-the-stage-claim-recover`; absolute paths / `git -C <worktree>` only; before any write assert `git -C <worktree> branch --show-current` is that branch. Never touch `main`, never force-push, never merge.
- After each task's commit run exactly `git -C /Users/francis/playground/agent-pm/work/TASK-70/worktrees/TASK-70-the-assignee-is-the-stage-claim-recover push -u origin TASK-70-the-assignee-is-the-stage-claim-recover`.
- Follow the repo's `CLAUDE.md`: terse code, comments only for a gotcha, docstrings one line; commit messages `TASK-70: <what>`.
- Prototype: no new validation, config or abstraction beyond the spec; remove what the project model needed.
- Role accounts are matched by Linear user id (`pipeline.role_ids`); the launcher matches `--assignee` to `account` case-insensitively.
- No harness mutation (claim, Recover requeue, attempt cap, promote) sends `assigneeId`, except promote's `issueCreate` of the child.
- Tests never touch network, Keychain or Claude. Tests that call `runnable(cfg)` without `root` use the repo's real `roles/` and `tasks/` (researcher → deep-research, no prefix; pm → product-design, `PRD`; engineer → engineering, `ENG`; accounts `frank.agent.w+<role>@gmail.com`); read accounts via `pipeline.registry()[0][<role>].account`, never hard-code them.
- Task 1 changes `runnable()`'s shape, so `test_router`, `test_launch` and `test_promote` fail until Tasks 2–4; each task runs only the suites it names.

## Review Focus

- An issue assigned to a role account but in no project: never queued, recovered or promoted (`project: {null: false}`); covered in Task 2 and Task 4 fakes.
- A Handoff source assigned to a role without `next` (engineer): left in Handoff, no mutation (Task 4).
- `--assignee` email differing in case from `account`: launcher still resolves the role (Task 3).
- A role account missing in Linear: router and promote stop before any change with `roles/<role>.toml: account '…' not found in Linear` (Tasks 1, 2).
- A leftover `[projects]` table in `pipeline.toml`: rejected as an unknown key (Task 1).

---

### Task 1: pipeline config by role

**Files:**
- Modify: `scripts/pipeline.py`, `pipeline.toml`, `scripts/tests/board_ids.py`
- Test: `scripts/tests/test_pipeline.py`, `scripts/tests/test_prune.py` (must stay green)

**Interfaces:**
- Produces: `TOP_KEYS = {"team", "states", "human_members", "harness_key", "roles"}`; `PIPELINE_ROLE_KEYS = {"next", "require_instructions"}` (replaces `PROJECT_KEYS`); `Run(task_name, task, charter, instructions, memory, read_only, key, account)`; `runnable(cfg, root=ROOT) -> {role: Run}`; `stage_order(cfg) -> {role: int}`; `role_ids(gql, runs) -> {user id: role}`; `Team(id, name, states)` (no `projects`); `Q_TEAM` without the projects sub-query; `board_ids.team_node(state_ids=None)` (no `projects`).

- [ ] **Step 1: Rewrite the config tests (failing).** In `test_pipeline.py`:
  - `BASE` becomes roles-keyed and the `Config` tests follow:
    ```python
    BASE = HEADER + """[roles.researcher]
    next = "pm"
    [roles.pm]
    next = "engineer"
    [roles.engineer]
    [roles.solo]
    """
    ```
    `test_stage_order` expects `{"researcher": 0, "pm": 1, "engineer": 2, "solo": 0}`; `test_cycle_rejected` appends `'[roles.a]\nnext = "b"\n[roles.b]\nnext = "a"\n'`; delete `test_next_without_prefix_rejected` (moves to `Runnable`); `test_ids_required` slices the body from `"[roles"`.
  - `Runnable`: `PROJECTS` becomes `ROLES = HEADER + '[roles.researcher]\nnext = "engineer"\n'`, and `tasks/engineering.toml` in `FILES` gains `prefix = "ENG"\n`. Rewrite `test_runs` to expect `sorted(runs) == ["engineer", "researcher"]` with `runs["researcher"].task_name == "deep-research"`, `runs["engineer"].task_name == "engineering"`, charters `roles/<role>.md`, `account` `"r@x.com"` / `"e@x.com"`. Replace the project-specific tests (`test_old_tables_rejected`, `test_project_keys_rejected`, `test_load_config_ignores_old_tables`, `test_role_and_task_together`, `test_unknown_role_or_task`, `test_load_config_does_not_need_files`, `test_repo_read_only`, `test_unused_role_validated`) with role equivalents, keeping each test's intent. New tests (messages exact):
    ```python
    def test_projects_table_rejected(self):
        self.rejects("pipeline.toml has unknown keys: projects", text=ROLES + '[projects.p]\nnext = "q"\n')

    def test_next_names_undefined_role(self):
        self.rejects("pipeline.toml: next of 'researcher' names undefined role 'ghost'",
                     text=ROLES.replace('next = "engineer"', 'next = "ghost"'))

    def test_next_role_default_task_needs_prefix(self):
        self.write("tasks/engineering.toml", ENGINEERING.replace('prefix = "ENG"\n', ""))
        self.rejects("pipeline.toml: next of 'researcher' is role 'engineer', whose default task 'engineering' has no prefix")

    def test_roles_table_needs_a_role_pair(self):
        self.rejects("pipeline.toml: [roles.ghost] has no roles/<role>.md + .toml pair", text=ROLES + "[roles.ghost]\n")

    def test_roles_table_unknown_keys(self):
        self.rejects("pipeline.toml: [roles.researcher] has unknown keys: role, task",
                     text=ROLES + 'role = "x"\ntask = "y"\n')

    def test_default_task_is_first(self):
        self.write("roles/engineer.toml", self.read_role("engineer").replace('tasks = ["engineering"]', 'tasks = ["engineering", "deep-research"]'))
        self.assertEqual(self.runs()["engineer"].task_name, "engineering")

    def test_role_ids(self):
        runs = self.runs()
        gql = lambda q, **v: {"users": {"nodes": [{"id": "u-" + v["e"]}]}}
        self.assertEqual(pipeline.role_ids(gql, runs), {"u-e@x.com": "engineer", "u-r@x.com": "researcher"})
        with self.assertRaises(SystemExit) as cm:
            pipeline.role_ids(lambda q, **v: {"users": {"nodes": []}}, {"engineer": runs["engineer"]})
        self.assertEqual(str(cm.exception.code), "roles/engineer.toml: account 'e@x.com' not found in Linear")
    ```
    (`read_role` = read `roles/<name>.toml` from `self.root`; add it beside `write`.) The `{repo}` read_only test now expects `roles/engineer.toml: read_only {repo} needs default task 'engineering' with repo_from_issue and no allowed_tools`.
  - `RealConfig.test_three_runs`: keys become `"researcher"`, `"pm"`, `"engineer"` (same tuples); add `self.assertEqual({k: r.account for k, r in runs.items()}, {k: pipeline.registry()[0][k].account for k in runs})`, `pipeline.stage_order(cfg) == {"researcher": 0, "pm": 1, "engineer": 2}` and `cfg["roles"]["pm"]["require_instructions"] is False`.
  - `TeamCheck.test_ok`: `team_node()` and `pipeline.Team(TEAM, "Team", dict(STATES))`; assert `"projects" not in query`.
- [ ] **Step 2: Run** `python3 -m unittest scripts/tests/test_pipeline.py` from the worktree root (or `discover -s scripts/tests -p test_pipeline.py`) — expect failures.
- [ ] **Step 3: Implement `pipeline.py`.**
  ```python
  TOP_KEYS = {"team", "states", "human_members", "harness_key", "roles"}
  PIPELINE_ROLE_KEYS = {"next", "require_instructions"}
  ```
  `load_config` (docstring: `"""Checks every consumer needs; the role checks are in runnable(), which every consumer calls."""`):
  ```python
      roles = cfg.setdefault("roles", {})
      for name in roles:
          nxt, seen = roles[name].get("next"), {name}
          while nxt:
              if nxt in seen:
                  raise SystemExit(f"pipeline.toml: the next chain from {name!r} has a cycle")
              seen.add(nxt)
              nxt = roles.get(nxt, {}).get("next")
      return cfg
  ```
  `Run` gains `account: str` (last field; docstring: `"""A role's default task, resolved from roles/ and tasks/; every path is absolute. key is the role's Keychain service name, account its Linear email."""`). `runnable`:
  ```python
  def runnable(cfg, root=ROOT):
      """{role: Run} of every role, running its default task; a broken pipeline.toml, role or task stops the caller (fail loud)."""
      if extra := sorted(set(cfg) - TOP_KEYS):
          raise SystemExit(f"pipeline.toml has unknown keys: {', '.join(extra)}; {SETTINGS}")
      roles, tasks = registry(root)
      for name, r in roles.items():
          if r.key == cfg["harness_key"]:
              raise SystemExit(f"roles/{name}.toml: key {r.key!r} is pipeline.toml's harness_key")
      for name, p in cfg["roles"].items():
          if name not in roles:
              raise SystemExit(f"pipeline.toml: [roles.{name}] has no roles/<role>.md + .toml pair")
          if extra := sorted(set(p) - PIPELINE_ROLE_KEYS):
              raise SystemExit(f"pipeline.toml: [roles.{name}] has unknown keys: {', '.join(extra)}; {SETTINGS}")
          nxt = p.get("next")
          if not nxt:
              continue
          if nxt not in roles:
              raise SystemExit(f"pipeline.toml: next of {name!r} names undefined role {nxt!r}")
          if not tasks[roles[nxt].tasks[0]].get("prefix"):
              raise SystemExit(f"pipeline.toml: next of {name!r} is role {nxt!r}, whose default task {roles[nxt].tasks[0]!r} has no prefix")
      out = {}
      for name, r in roles.items():
          task = r.tasks[0]
          t = tasks[task]
          if REPO in r.read_only and (not t.get("repo_from_issue") or "allowed_tools" in t):
              raise SystemExit(f"roles/{name}.toml: read_only {REPO} needs default task {task!r} with repo_from_issue and no allowed_tools")
          out[name] = Run(task, t, os.path.join(root, "roles", f"{name}.md"), os.path.join(root, "tasks", f"{task}.md"),
                          r.memory, r.read_only, r.key, r.account)
      return out
  ```
  `role_ids` after `humans`:
  ```python
  def role_ids(gql, runs):
      """{Linear user id: role} of the runs' accounts; an account not found in Linear stops the caller."""
      out = {}
      for name, run in runs.items():
          uid = user_id(gql, run.account)
          if not uid:
              raise SystemExit(f"roles/{name}.toml: account {run.account!r} not found in Linear")
          out[uid] = name
      return out
  ```
  `Team` drops `projects` (docstring `{logical state: state id}`), `Q_TEAM` drops `projects(first: 50) { nodes { id name } }`, `team()` returns `Team(t["id"], t["name"], {k: cfg["states"][k] for k in STATES})`. `stage_order` iterates `cfg["roles"]` (same algorithm; docstring `{role: position in its next chain}; roles outside a chain are 0.`). Module docstring unchanged.
- [ ] **Step 4: `pipeline.toml`.** Replace the `[projects…]` block (and its comment) with:
  ```toml
  # Each role's place in the pipeline, keyed by role name (roles/<role>.md + roles/<role>.toml);
  # an issue assigned to the role's account runs the role's default task.
  [roles.researcher]
  next = "pm"                        # promote hands the role's Handoff issues to this role

  [roles.pm]
  next = "engineer"
  require_instructions = false       # a PRD carries enough context; Handoff comments are optional

  [roles.engineer]                   # no next: the last stage
  ```
- [ ] **Step 5: `board_ids.py`.** `team_node(state_ids=None)` without `projects`; the `HEADER` comment says fixtures may append `[roles]` tables.
- [ ] **Step 6: Run** `python3 -m unittest discover -s scripts/tests -p 'test_pipeline.py'` and `-p 'test_prune.py'` and `-p 'test_eng.py'` — all pass.
- [ ] **Step 7: Commit** `TASK-70: pipeline.toml keys the pipeline by role` (pipeline.py, pipeline.toml, board_ids.py, test_pipeline.py), then push.

### Task 2: router claims, orders and recovers by assignee

**Files:**
- Modify: `scripts/router.py`
- Test: `scripts/tests/test_router.py`

**Interfaces:**
- Consumes: `runnable`, `stage_order`, `role_ids`, `team` (Task 1).
- Produces: launch argv `[sys.executable, LAUNCH, "--issue", ID, "--url", URL, "--project", <issue project id>, "--assignee", <issue assignee email>, "--sid", SID] + mode`; CLI `--pick [--role ROLE] [RUNS_LOG]`.

- [ ] **Step 1: Migrate the fixtures (failing).** In `test_router.py`:
  ```python
  ROLE = {name: r.account for name, r in pipeline.registry()[0].items()}  # the repo's roles/
  AGENT, USER = "agent", "user"   # history actor ids: an agent account, a human_members user
  CONFIG = HEADER + '[roles.researcher]\nnext = "pm"\n[roles.pm]\nnext = "engineer"\n[roles.engineer]\n'

  def who(role):
      """assignee node of a role account, or of someone else for a non-role name."""
      return None if role is None else {"id": f"u-{role}", "email": ROLE.get(role, f"{role}@x.com")}
  ```
  `issue(ident, state, role=None, updated=None, priority=0, created=…, project=DR)` stores `"assignee": who(role)`; `project=None` stores `"project": None`. `FakeLinear`: drop the `viewer` branch (a `viewer` query must hit `raise AssertionError`); `team_node(self.state_ids)`; the users lookup also answers role accounts case-insensitively (`{a.lower(): f"u-{r}" for r, a in ROLE.items()}`), except emails in a new `self.missing = set()`; the issue update records `v.get("u") or {"stateId": v["s"]}` and applies `assigneeId` only if present; the issues filter returns issues where `f["team"]["id"]["eq"] == TEAM`, `f["project"] == {"null": False}` and `i["project"]`, `i["assignee"] and i["assignee"]["id"] in f["assignee"]["id"]["in"]`, and the state matches. Existing tests replace `ME` as assignee with `"researcher"` (and `project=PD` issues with `"pm"`), history actor `ME` with `AGENT`; assertions that the requeue/claim sets the assignee now expect it unchanged (`who("researcher")`). Delete `PD_RUNNABLE`, `test_non_runnable_project_ignored`, `test_renamed_project_still_matches`, `test_runnable_project_missing_in_linear_exits`, `test_pick_project_filter`.
- [ ] **Step 2: New tests** (in the matching test classes):
  ```python
  def test_queue_only_role_accounts_across_projects(self):
      fake = FakeLinear([issue("TASK-1", "Todo"), issue("TASK-2", "Todo", "someone"), issue("TASK-3", "Todo", "pm", project=None),
                         issue("TASK-4", "Todo", "engineer", project=PD, priority=3)])
      self.assertEqual(self.run_main(fake, "--claim")[1], f"TASK-4 https://linear.app/x/TASK-4 {PD}")
      self.assertEqual([i for i in ("TASK-1", "TASK-2", "TASK-3") if fake.issues[i]["state"] != "Todo"], [])
      flt = next(v["f"] for q, v in fake.queries if "issues(filter" in q)
      self.assertEqual((flt["team"], flt["project"], sorted(flt["assignee"]["id"]["in"])),
                       ({"id": {"eq": TEAM}}, {"null": False}, sorted(f"u-{r}" for r in ROLE)))

  def test_claim_priority_then_later_role_then_age(self):
      fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=2, created="2026-09-01T00:00:00Z"),
                         issue("TASK-2", "Todo", "engineer", priority=2, created="2026-09-03T00:00:00Z"),
                         issue("TASK-3", "Todo", "pm", priority=2, created="2026-09-02T00:00:00Z"),
                         issue("TASK-4", "Todo", "engineer", priority=2, created="2026-09-02T00:00:00Z")])
      self.assertEqual(self.run_main(fake, "--claim")[1].split()[0], "TASK-4")

  def test_claim_only_moves_to_in_progress(self):
      fake = FakeLinear([issue("TASK-1", "Todo", "pm")])
      self.run_main(fake, "--claim")
      (q, v), = fake.mutations
      self.assertNotIn("assignee", q + repr(v))
      self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("In Progress", who("pm")))

  def test_recover_requeue_keeps_assignee(self):
      fake = FakeLinear([issue("TASK-1", "In Progress", "engineer", updated=ago(hours=3), project=PD)])
      self.run_main(fake, "--plan")
      self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("Todo", who("engineer")))
      self.assertFalse(any("assigneeId" in repr(v) for _, v in fake.mutations))

  def test_recover_skips_other_assignees(self):
      fake = FakeLinear([issue("TASK-1", "In Progress", "someone", updated=ago(hours=3)),
                         issue("TASK-2", "In Progress", None, updated=ago(hours=3))])
      self.run_main(fake, "--plan")
      self.assertEqual(fake.mutations, [])

  def test_pick_role_filter(self):
      fake = FakeLinear([issue("TASK-1", "Todo", "engineer", priority=1), issue("TASK-2", "Todo", "researcher", priority=4)])
      self.assertEqual(self.run_main(fake, "--pick", "--role", "researcher")[1], "TASK-2 https://linear.app/x/TASK-2")
      flt = next(v["f"] for q, v in fake.queries if "issues(filter" in q)
      self.assertEqual(flt["assignee"]["id"]["in"], ["u-researcher"])

  def test_pick_project_is_a_usage_error(self):
      self.assertEqual(self.run_main(FakeLinear([]), "--pick", "--project", "p-dr")[0], 2)

  def test_pick_unknown_role_exits(self):
      with self.assertRaises(SystemExit):
          self.run_main(FakeLinear([]), "--pick", "--role", "ghost")

  def test_role_account_missing_in_linear_exits(self):
      fake = FakeLinear([issue("TASK-1", "Todo", "pm")])
      fake.missing = {ROLE["pm"].lower()}   # the users lookup returns no node for these
      with self.assertRaises(SystemExit) as cm:
          self.run_main(fake, "--claim")
      self.assertIn("roles/pm.toml: account", str(cm.exception.code))
      self.assertEqual(fake.mutations, [])
  ```
  Adapt `run_main`'s return shape to the file's existing helper (it returns `(rc, stdout)` today — check and match). In the tick tests, the launch call asserts `--project`, the issue's project id, `--assignee`, the assignee's email for both a new and a resumed run.
- [ ] **Step 3: Run** `python3 -m unittest discover -s scripts/tests -p 'test_router.py'` — expect failures.
- [ ] **Step 4: Implement `router.py`.** Imports: `role_ids` in, keep `runnable`, `stage_order`, `team`. `Board.__init__`:
  ```python
      def __init__(self, gql, entries, tdir, now, dry, cfg, only=None):
          self.gql, self.entries, self.tdir, self.now, self.dry = gql, entries, tdir, now, dry
          self.hist = {}
          runs = runnable(cfg)
          if only is not None and only not in runs:
              raise SystemExit(f"no role {only!r} in roles/")
          self.stage = stage_order(cfg)
          t = team(gql, cfg)
          self.team, self.states = t.id, t.states
          self.humans = set(humans(gql, cfg))
          self.emails = cfg.get("human_members") or []
          self.roles = role_ids(gql, {r: run for r, run in runs.items() if only in (None, r)})

      def issues(self, state):
          flt = {"team": {"id": {"eq": self.team}}, "project": {"null": False},
                 "assignee": {"id": {"in": list(self.roles)}}, "state": {"id": {"eq": self.states[state]}}}
          return self.gql("""query($f: IssueFilter) { issues(filter: $f, first: 100) {
                      nodes { id identifier url priority createdAt updatedAt project { id name } assignee { id email } } } }""",
                          f=flt)["issues"]["nodes"]

      def later(self, issue):
          return -self.stage.get(self.roles[issue["assignee"]["id"]], 0)
  ```
  `comment_and_move(self, issue, body, state)` updates `u={"stateId": self.states[state]}`. `recover` uses `self.issues("in_progress")` and both requeues call `self.comment_and_move(issue, INTERRUPTED, "todo")`; docstring `"""Walk the role accounts' In Progress issues; …"""`. The claim:
  ```python
              self.gql("mutation($i: String!, $s: String!) { issueUpdate(id: $i, input: { stateId: $s }) { success } }",
                       i=issue["id"], s=self.states["in_progress"])
  ```
  The not-found log: `f"pick: {only} is not a Todo issue assigned to a role account"`; the pick comment `# Pick: highest priority first, then later role, then oldest.` `tick` builds the launch argv with `"--project", project["id"], "--assignee", issue["assignee"]["email"]`. `main`'s `--pick` parses `--role` exactly as it parsed `--project`. `USAGE` and the docstring: `--pick [--role ROLE] [RUNS_LOG]  Recover, then Pick + Claim (only ROLE's issues if given); print "<ID> <url>" (manual use).`; docstring line 2: `Router: decides what runs next among the team's issues assigned to role accounts, then calls launch.py.`
- [ ] **Step 5: Run** the router suite — all pass; `python3 -m unittest discover -s scripts/tests -p 'test_pipeline.py'` still passes.
- [ ] **Step 6: Commit** `TASK-70: router claims and recovers by assignee`, then push.

### Task 3: launcher resolves the role from the assignee

**Files:**
- Modify: `scripts/launch.py`
- Test: `scripts/tests/test_launch.py`

**Interfaces:**
- Consumes: `runnable(cfg, root) -> {role: Run}` with `Run.account` (Task 1); the router's argv (Task 2).
- Produces: CLI `launch.py --issue ID --url URL --project PROJECT_ID --assignee EMAIL --sid SID --mode new|resume [--k K]`.

- [ ] **Step 1: Migrate tests (failing).** `CONFIG = HEADER + 'human_members = ["me@x.com"]\n'`. `args(mode="new", assignee="r@x.com", project="p-dr")` adds `"--assignee", assignee`. Every former `project="p-eng"` becomes `assignee="e@x.com"`; the real-config class's `launch(project, …)` becomes `launch(role, …)` passing `--project p-x --assignee pipeline.registry()[0][role].account`. Replace `test_unknown_or_non_runnable_project` with:
  ```python
  def test_assignee_not_a_role_account(self):
      for assignee in ("nobody@x.com", ""):
          self.assertEqual(self.run_launch(*self.args(assignee=assignee)), 2)
          self.assertIn("is not a role account", self.err)
      self.assertEqual(self.calls, [])

  def test_assignee_matches_ignoring_case(self):
      self.assertEqual(self.run_launch(*self.args(assignee="R@X.com")), 0)
      self.assertIn("LINEAR_KEYCHAIN_SERVICE=k-researcher", self.exports())

  def test_project_only_fills_the_prompt(self):
      self.assertEqual(self.run_launch(*self.args(project="p-anything")), 0)
      self.assertIn(" Project: p-anything.", self.claude()[2])
  ```
- [ ] **Step 2: Run** `python3 -m unittest discover -s scripts/tests -p 'test_launch.py'` — expect failures.
- [ ] **Step 3: Implement.** Add `"--assignee"` to the required args loop. Replace the project lookup:
  ```python
      jobs = runnable(cfg, root)
      role = next((r for r, j in jobs.items() if j.account.lower() == a.assignee.lower()), None)
      if role is None:
          print(f"launch.py: {a.assignee!r} is not a role account", file=sys.stderr)
          return 2
      job = jobs[role]
  ```
  Docstring: usage line with `--assignee EMAIL`; `The role is the one whose account is EMAIL, the issue's assignee (on a resume, its assignee now); it runs the role's default task. PROJECT_ID only fills the prompt's Project:. Exits 2 for an assignee that is not a role account or a config error …` (keep the rest).
- [ ] **Step 4: Run** the launch suite — all pass.
- [ ] **Step 5: Commit** `TASK-70: launcher takes the role from the assignee`, then push.

### Task 4: promote hands off within the project to the next role

**Files:**
- Modify: `scripts/promote.py`
- Test: `scripts/tests/test_promote.py`

**Interfaces:**
- Consumes: `runnable`, `role_ids`, `team` (Task 1).
- Produces: `child_id(source_id, target, handoff_at)` (target = next role name); `Q_HANDOFF` with `$a: [ID!]`.

- [ ] **Step 1: Migrate tests (failing).**
  ```python
  ROLE = {name: r.account for name, r in pipeline.registry()[0].items()}
  CONFIG = HEADER + 'human_members = ["me@x.com"]\n[roles.researcher]\nnext = "pm"\n'
  ```
  `FakeLinear.add(ident, project="Deep Research", state="Handoff", role="researcher", **kw)` stores `assignee={"id": f"u-{role}"}` (`role=None` → `None`); `project=None` stores `None`. The users lookup answers `ROLE` accounts case-insensitively with `u-<role>`. The `Q_HANDOFF` branch records vars and returns Handoff issues with a project whose `assignee["id"]` is in `v["a"]`; `team_node(self.state_ids)`. Tests that set `projectId` expectations now expect the source's project (`"p-dr"`) and `assigneeId == "u-pm"`. Rework the project-model tests:
  - `test_project_without_next_untouched` → `test_role_without_next_untouched`: a `role="pm"` source under `CONFIG` (pm has no `next` there) stays in Handoff, no mutations.
  - `test_config_only_extension` → config `CONFIG + '[roles.pm]\nnext = "engineer"\n'`, `role="pm"` source titled `"PRD: Title DR-1"` → child `("p-dr", "u-engineer", "ENG: Title DR-1")`.
  - `test_instructions_optional` / `test_optional_instructions_still_copied`: `[roles.pm]\nnext = "engineer"\nrequire_instructions = false\n`, `role="pm"`, expected title `ENG: Title PD-1`, project `p-pd`.
  - `test_renamed_projects_still_match`: delete. `test_next_without_prefix_exits` / `test_next_without_projects_entry_exits` → `CONFIG.replace('next = "pm"', 'next = "ghost"')` exits; `CONFIG + '[roles.pm]\nnext = "researcher"\n'` exits (deep-research has no prefix) — both before any mutation.
  - New:
  ```python
  def test_child_in_same_project_assigned_to_next_role(self):
      src = self.ready(project="Product Design", title="Title DR-1")
      self.run_main()
      (child,) = self.fake.children.values()
      self.assertEqual((child["projectId"], child["assigneeId"], child["title"], child["stateId"]),
                       ("p-pd", "u-pm", "PRD: Title DR-1", STATES["Todo"]))
      self.assertEqual(child["id"], promote.child_id("DR-1", "pm", ago(30)))
      self.assertEqual(src["state"], "Done")
      self.assertFalse(any("assigneeId" in repr(v) for q, v in self.fake.mutations if q != promote.M_CREATE))

  def test_handoff_scans_role_accounts_in_projects(self):
      self.ready("DR-1", role=None)
      self.ready("DR-2", role="someone")
      self.ready("DR-3", project=None)
      self.run_main()
      self.assertEqual(self.fake.mutations, [])
      self.assertEqual(sorted(self.fake.handoff_vars["a"]), sorted(f"u-{r}" for r in ROLE))
      self.assertIn("project: { null: false }", promote.Q_HANDOFF)
      self.assertIn("assignee: { id: { in: $a } }", promote.Q_HANDOFF)

  def test_engineer_handoff_left_alone(self):
      src = self.ready(role="engineer")
      self.run_main()
      self.assertEqual((self.fake.mutations, src["state"]), ([], "Handoff"))

  def test_dry_run_names_role_and_project(self):
      self.ready()
      self.run_main("--dry-run")
      self.assertIn("promote DR-1 -> new pm issue in Deep Research", self.out)
  ```
  (For `project=None` make `add` skip the `PROJECTS` lookup.)
- [ ] **Step 2: Run** `python3 -m unittest discover -s scripts/tests -p 'test_promote.py'` — expect failures.
- [ ] **Step 3: Implement `promote.py`.**
  ```python
  from pipeline import CONFIG, linear_gql, load_config, parse_time, role_ids, runnable, team  # noqa: E402

  Q_HANDOFF = """query($t: ID, $s: ID, $a: [ID!]) { issues(filter: { team: { id: { eq: $t } }, state: { id: { eq: $s } },
    project: { null: false }, assignee: { id: { in: $a } } }, first: 100) {
    nodes { id identifier url title priority createdAt project { id name } assignee { id } attachments { nodes { title url } } } } }"""

  def child_id(source_id, target, handoff_at):
      key = f"{source_id}/{target}/{handoff_at}".encode()
      …
  ```
  `Promoter.__init__`: `self.team = team(gql, cfg)`, `self.states = self.team.states`, `self.humans` as today, then `self.runs = runnable(cfg)`, `self.roles = role_ids(gql, self.runs)`, `self.ids = {r: i for i, r in self.roles.items()}`; the next-project check goes. `run()`:
  ```python
          issues = self.gql(Q_HANDOFF, t=self.team.id, s=self.states["handoff"], a=list(self.roles))["issues"]["nodes"]
          work = []
          for src in issues:
              role = self.roles[src["assignee"]["id"]]
              nxt = self.cfg["roles"].get(role, {}).get("next")
              if nxt:
                  … work.append((self.moves(src, detail), src, detail, role, nxt))
          for (cutoff, first, latest), src, detail, role, nxt in sorted(work, key=lambda w: w[0][2]):
              …
                  self.promote(src, detail, role, nxt, cutoff, first, found)
  ```
  `promote(self, src, detail, role, nxt, cutoff, first, found)`: `required = self.cfg["roles"].get(role, {}).get("require_instructions", True)`; `cid = child_id(src["id"], nxt, first)`; dry-run line `f"promote {src['identifier']} -> new {nxt} issue in {src['project']['name']}"`; `issueCreate` input:
  ```python
                  "id": cid, "teamId": self.team.id, "projectId": src["project"]["id"], "assigneeId": self.ids[nxt],
                  "stateId": self.states["todo"], "priority": src["priority"],
                  "title": child_title(self.runs[nxt].task["prefix"], self.runs[role].task.get("prefix"), src["title"]),
  ```
  Docstring: `Promote issues in Handoff to the next role (docs/specs/2026-09-27-promote-design.md).` / `For each Handoff issue in a project, assigned to a role account whose [roles.<role>] in pipeline.toml has a next: create that role's issue in the same project, assigned to its account, in Todo with the source links and the human instructions, relate it, and move the source to Done.`
- [ ] **Step 4: Run** the promote suite, then `python3 -m unittest discover -s scripts/tests` — all pass.
- [ ] **Step 5: Commit** `TASK-70: promote hands off in the same project to the next role`, then push.

### Task 5: rules and docs

**Files:**
- Modify: `tasks/deep-research.md`, `tasks/product-design.md`, `tasks/engineering.md`, `README.md`, `CLAUDE.md`

- [ ] **Step 1: Tasks.** `tasks/deep-research.md` step 1: `python3 ../scripts/router.py --pick --role researcher`; step 4: `Else set In Progress and comment that research started.` (drop "assignee `viewer`"). In each `tasks/*.md` `## Board`, the first bullet's "The <stage> project" becomes "Issues assigned to your role account, in any project" (rest of the bullet unchanged).
- [ ] **Step 2: README.** Intro: each Linear project is a product; an issue's assignee — a role account (researcher, pm, engineer) — is its stage. The stage table's first column becomes the role (researcher / pm / engineer). "Using the board": create the issue in the product's project, assigned to the role account that should do it, in Todo (unassigned issues are never picked); Approve: promote creates the next role's issue in the same project, assigned to that role. Schedule: "priority, then later role, then oldest". Operating: add `python3 scripts/router.py --pick --role researcher   # recover, then claim the role's top Todo issue`. Configuration: `pipeline.toml` holds the team, states, `human_members`, `harness_key`, and per role (`[roles.<role>]`) its `next` role and `require_instructions`. Add `## Cutover (TASK-62 PR-C)` with the PRD step 7 list: merge without deploying; create a project per product; stop the pipeline (no `agent-pm` tmux session; every `start` in `logs/runs.log` has an `end`; don't run the router by hand); reassign every open issue (Todo, In Progress, In Review, Handoff) by its stage project — 1-Research → researcher, 2-Product Design → pm, 3-Engineering → engineer — or it is never picked, recovered or handed off again; `git pull --ff-only` in `~/playground/agent-pm`; `python3 scripts/router.py --now --dry-run` shows the role accounts' Todo issues, and after the next promote tick `logs/promote.log` has no error.
- [ ] **Step 3: CLAUDE.md.** Intro: launchd scripts pick Todo issues assigned to a role account (any project; project = product) and run that role's default task. Architecture: router queue/Recover by role account (claim and requeue keep the assignee); launcher's role from `--assignee`; promote hands off to `[roles.<role>].next` in the same project, assigned to that role's account, child id a hash of source, next role and handoff time; `pipeline.py` bullet mentions `role_ids`, and that promote now validates `roles/`/`tasks/` too (a broken file stops hand-offs as well as runs). "Roles, tasks, rules": every `roles/<role>` pair is runnable (its default task is the first in `tasks`); `pipeline.toml` `[roles.<role>]` holds `next` and `require_instructions`. Gotchas: `pipeline.toml` refers to the team and states by Linear id; roles by name.
- [ ] **Step 4: Run** `python3 -m unittest discover -s scripts/tests` — all pass; `grep -rn "\-\-pick --project\|\[projects" README.md CLAUDE.md tasks roles scripts pipeline.toml` → nothing.
- [ ] **Step 5: Commit** `TASK-70: docs and tasks describe the assignee model and the cutover`, then push.

## Verification (S6)

- `python3 -m unittest discover -s scripts/tests` from the worktree root: all pass.
- `python3 -c 'import sys; sys.path.insert(0, "scripts"); import pipeline; c = pipeline.load_config(); print(sorted(pipeline.runnable(c)), pipeline.stage_order(c))'` → `['engineer', 'pm', 'researcher'] {'researcher': 0, 'pm': 1, 'engineer': 2}`.
- `python3 scripts/router.py --pick --project x` exits 2 with the usage line (no Linear call before the usage check).
- The live `router.py --now --dry-run` needs the harness key and Linear; its queue filter is pinned by `test_queue_only_role_accounts_across_projects`.


## Progress
- decision(launcher input): chose `--assignee <email>` over `--role` - FR-13 literal; router resolves the same accounts by id, so the launcher match cannot miss; dissent: ops lens preferred `--role` (fail before claim).
- decision(account matching): chose resolving role accounts to user ids (`role_ids`, stop on unknown) over `email in` filter - Linear `in` is case-sensitive, promote needs ids; dissent: none.
- decision(child id key): chose next role name over the source's project id - project no longer distinguishes the hop; dissent: ops lens preferred project id (stable against toml edits/reassign).
- decision(deep-research step 4 / task Board bullets): drop "assignee viewer" and "The <stage> project" - FR-8 and the stage-project model they encode; dissent: none.
- S3 panel: core=[architecture,spec-fitness] +optional=[] (security dropped: keys/launcher identity unchanged) transport=Workflow
- S3 r0: architecture=PASS spec-fitness=PASS -> converged; folded NB: load_config docstring, CLAUDE.md promote validates registry
- S4: plan written (5 tasks: pipeline, router, launch, promote, docs); tasks 2-4 depend on task 1's runnable/role_ids.
- S5: tasks 1-5 done via subagent-driven-development, each task review Approved (065840e, 69f5389, ffdb2ae, fb2666d, 7d44f0d); ruling: promote prefix-exit test uses [roles.engineer] next=researcher (plan's config was a cycle).
- S6: unittest 280 OK, no warnings; read-only live check: Board(dry) + Promoter init against Linear OK, todo queue = 7 role-assigned issues across projects Agent PM/MISC, filters accepted.
- S7 panel: core=[correctness,requirement-fidelity,doc] +optional=[test,code-quality] (performance: 3 users queries/tick marginal; architecture: structure reviewed at S3 and follows spec) transport=Workflow
- S7 r0: correctness=PASS requirement-fidelity=PASS doc=FAIL test=PASS code-quality=PASS -> doc#1 README cutover: dry-run "shows" Todo issues (only logs plan/count); doc#2 README "unassigned issue never picked" too narrow (any issue not assigned to a role account); fix dispatched (pre-fix HEAD f1acda3)
- S7 fix r0->r1: fixer=a54e6a7714b5c757f commit 4d5a657 (doc#1 README.md:89, doc#2 README.md:13,87; NB: README:50,77, CLAUDE.md:25, pipeline.py:308); r1 re-review = all 5 (cores + *.py touched)
- S7 r1: doc=PASS (doc#1, doc#2 RESOLVED) correctness=PASS requirement-fidelity=PASS test=PASS code-quality=PASS -> converged
- S8: skipped (keep the commits, per the requirement)
- Residual non-blocking: a resume after reassigning an In Progress issue to another role runs as the new role (launch.py docstring); an In Progress issue reassigned to a non-role account is never recovered (FR-9); a non-table `[roles]` entry raises a Python error, not a SystemExit; two roles sharing an account collapse (non-goal); tests' fakes reimplement the Linear filters (filter shapes asserted); the order test checks only the winner.
