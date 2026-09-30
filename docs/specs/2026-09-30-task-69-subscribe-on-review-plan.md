# TASK-69: In Review subscribes the humans — plan

RESUME: phase=S5 worktree=/Users/francis/playground/agent-pm/work/TASK-69/worktrees/TASK-69-in-review-subscribes-the-humans-instead branch=TASK-69-in-review-subscribes-the-humans-instead base_ref=909c4d368340a06a43184710c43bc80f8d484672 review_round=0 spec_file=docs/specs/2026-09-30-task-69-subscribe-on-review-design.md

## Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every move to In Review subscribes the `human_members` and keeps the assignee; nothing assigns a reviewer.

**Architecture:** Router (`Board.comment_and_move`) and promote (`Promoter.move`) call `issueSubscribe(id:, userEmail:)` once per `human_members` email before the In Review state change, each in its file's idiom; `pipeline.reviewer()` and all `assigneeId` on In Review go. Rules (principles, engineering task), the launch prompt tail and docs follow.

**Tech Stack:** Python 3.11 stdlib, `unittest` (`python3 -m unittest discover -s scripts/tests`; no network, Keychain or Claude).

**Spec:** `docs/specs/2026-09-30-task-69-subscribe-on-review-design.md`

## Global Constraints

- Worktree `/Users/francis/playground/agent-pm/work/TASK-69/worktrees/TASK-69-in-review-subscribes-the-humans-instead`, branch `TASK-69-in-review-subscribes-the-humans-instead`; assert `git -C <worktree> branch --show-current` before any write; never touch `main`, never force-push, never merge.
- After each task: `git -C /Users/francis/playground/agent-pm/work/TASK-69/worktrees/TASK-69-in-review-subscribes-the-humans-instead push -u origin TASK-69-in-review-subscribes-the-humans-instead`.
- Commit messages: `TASK-69: <what>` (repo convention).
- Prototype: no new validation, config or abstraction; no code comments unless a gotcha.
- Mutation text exactly: `mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail: $e) { success } }`.
- Subscribe once per `cfg["human_members"]` email, in config order, before the In Review `issueUpdate`; none configured → no subscribe.
- Dispatch unchanged: the claim's `assigneeId: self.me` and Recover's `assigneeId=None` on Todo stay.

## Review Focus

- Two `human_members` → two subscribes, in order (tests use two emails).
- Subscribe fails in promote → issue stays in Handoff, no comment (Task 2 test).
- Moves to Todo / Done never subscribe (Task 1 and 2 tests).
- Empty `human_members` → no subscribe, move still happens (Task 1 test).
- Dry-run makes no subscribe (existing `test_no_mutations` in promote; router returns before mutating).

---

### Task 1: router subscribes the humans on In Review

**Files:**
- Modify: `scripts/router.py` (`Board.__init__` ~166-169, `Board.comment_and_move` ~206-214)
- Test: `scripts/tests/test_router.py` (`FakeLinear` ~52-69, tests ~451-478, `test_capped_todo_goes_to_review` ~573)

**Interfaces:** Consumes `pipeline.humans(gql, cfg)` (unchanged). Produces nothing new for other tasks.

- [ ] **Step 1: Tests.** In `FakeLinear.__call__`, resolve two known users and record subscribes:

```python
        if "users(filter" in query:
            return {"users": {"nodes": [{"id": u} for e, u in (("me@x.com", USER), ("b@x.com", "user-b")) if v["e"] == e]}}
```
```python
        if "mutation" in query:
            self.mutations.append((query, v))
            issue = self.issues[v["i"]]
            if "issueSubscribe" in query:
                issue.setdefault("subscribers", []).append(v["e"])
            elif "commentCreate" in query:
                issue.setdefault("comments", []).append(v["b"])
            else:
                ...  # unchanged
```

Replace `test_attempt_cap_assigns_reviewer`, rename the two others, extend two existing tests (add a module helper `ops`):

```python
def ops(fake):
    return [re.search(r"\{ (\w+)\(", q).group(1) for q, _ in fake.mutations]
```
```python
    def test_attempt_cap_moves_to_review(self):
        ...  # existing body, then:
        self.assertNotIn("subscribers", fake.issues["TASK-1"])

    def test_attempt_cap_subscribes_humans(self):
        self.config = self.write_config('human_members = ["me@x.com", "b@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "In Progress", ME, updated=ago(minutes=40))])
        for i, sid in enumerate(["a", "b", "c", "d"]):
            self.add("start", "TASK-1", sid, 400 - i * 50)
        self.run_main(fake, "--plan")
        t = fake.issues["TASK-1"]
        self.assertEqual((t["state"], t["assignee"], t["subscribers"]), ("In Review", ME, ["me@x.com", "b@x.com"]))
        self.assertEqual(ops(fake), ["issueSubscribe", "issueSubscribe", "commentCreate", "issueUpdate"])
        self.assertEqual(fake.mutations[-1][1]["u"], {"stateId": STATES["In Review"]})

    def test_interrupted_unassigns_and_subscribes_no_one(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "In Progress", ME, updated=ago(hours=5))])
        self.run_main(fake, "--plan")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("Todo", None))
        self.assertNotIn("subscribers", fake.issues["TASK-1"])

    def test_unknown_human_fails_loud(self):
        ...  # body of test_unknown_reviewer_fails_loud, unchanged
```

In `Claim.test_capped_todo_goes_to_review`, prepend `self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)` and add:

```python
        self.assertEqual((fake.issues["TASK-1"]["assignee"], fake.issues["TASK-1"]["subscribers"]), (None, ["me@x.com"]))
```

- [ ] **Step 2:** `python3 -m unittest discover -s scripts/tests -k test_router` → `test_attempt_cap_subscribes_humans` and `test_capped_todo_goes_to_review` FAIL (no subscribe; assignee is USER).
- [ ] **Step 3: Implement.** `Board.__init__`:

```python
        self.humans = set(humans(gql, cfg))
        self.emails = cfg.get("human_members") or []
```
(delete `ids = …` and `self.reviewer = …`). `comment_and_move`:

```python
    def comment_and_move(self, issue, body, state, **extra):
        if self.dry:
            return
        if state == "in_review":
            for email in self.emails:
                self.gql("mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail: $e) { success } }",
                         i=issue["id"], e=email)
        self.gql("mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }",
                 i=issue["id"], b=body)
        self.gql("mutation($i: String!, $u: IssueUpdateInput!) { issueUpdate(id: $i, input: $u) { success } }",
                 i=issue["id"], u={"stateId": self.states[state], **extra})
```
- [ ] **Step 4:** full suite passes: `python3 -m unittest discover -s scripts/tests`.
- [ ] **Step 5:** commit `TASK-69: router subscribes the humans on In Review, keeps the assignee`; push.

### Task 2: promote subscribes the humans; drop `pipeline.reviewer()`

**Files:**
- Modify: `scripts/promote.py` (import line 21, `M_REVIEW` line 39, `Promoter.__init__` line 70, `Promoter.move` ~185-191), `scripts/pipeline.py` (delete `reviewer()` ~340-342)
- Test: `scripts/tests/test_promote.py` (`FakeLinear.__call__` ~56-94, tests ~261-283, ~462-470, ~526-538)

**Interfaces:** Produces `promote.M_SUBSCRIBE` (module constant, the mutation text above). Removes `promote.M_REVIEW`, `pipeline.reviewer`.

- [ ] **Step 1: Tests.** In `FakeLinear.__call__`, delete the `users(filter` branch (promote must not look users up; any such query now raises `AssertionError`), and replace the `M_STATE`/`M_REVIEW` branch with:

```python
        if query == promote.M_SUBSCRIBE:
            self.issues[v["i"]].setdefault("subscribers", []).append(v["e"])
            return {"issueSubscribe": {"success": True}}
        if query == promote.M_STATE:
            self.issues[v["i"]]["state"] = next(n for n, i in STATES.items() if i == v["s"])
            return {"issueUpdate": {"success": True}}
```

Replace `test_bounce_assigns_reviewer`; extend two tests; add one; fix one:

```python
    def test_bounce_subscribes_humans(self):
        self.config = self.write_config(CONFIG.replace('["me@x.com"]', '["me@x.com", "b@x.com"]'))
        src = self.fake.add("DR-1", assignee="u-agent")
        self.fake.moved("DR-1", 60, "In Review")
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["In Review"])
        self.run_main()
        self.assertEqual((src["state"], src["assignee"], src["subscribers"]), ("In Review", "u-agent", ["me@x.com", "b@x.com"]))
        self.assertEqual([q for q, _ in self.fake.mutations],
                         [promote.M_SUBSCRIBE, promote.M_SUBSCRIBE, promote.M_STATE, promote.M_COMMENT])

    def test_promotion_leaves_assignee(self):
        ...  # existing body, then:
        self.assertNotIn("subscribers", src)

    def test_failing_over_grace_bounces(self):
        ...  # existing body, then:
        self.assertEqual(src["subscribers"], ["me@x.com"])

    def test_failed_subscribe_stays_in_handoff(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["In Review"])  # no instructions, under GRACE
        orig = self.fake.__call__

        def subscribe_fails(query, **v):
            if query == promote.M_SUBSCRIBE:
                raise SystemExit("linear api error: user not found")
            return orig(query, **v)
        self.fake = subscribe_fails
        self.run_main()
        self.assertEqual(src["state"], "Handoff")
        self.assertNotIn("posted", src)
        self.assertIn("handoff-error DR-1", self.out)
```
In `test_failed_bounce_move_posts_no_comment`: `if query == promote.M_STATE:`.

- [ ] **Step 2:** `python3 -m unittest discover -s scripts/tests -k test_promote` → FAIL (`M_SUBSCRIBE` missing / users query).
- [ ] **Step 3: Implement.** `promote.py`: import without `reviewer`; replace `M_REVIEW` with

```python
M_SUBSCRIBE = "mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail: $e) { success } }"
```
delete `self.reviewer = reviewer(gql, cfg)`; `move`:

```python
    def move(self, src, state):
        if self.dry:
            return
        if state == "in_review":
            for email in self.cfg.get("human_members") or []:
                ok(self.gql(M_SUBSCRIBE, i=src["id"], e=email), "issueSubscribe")
        ok(self.gql(M_STATE, i=src["id"], s=self.states[state]), "issueUpdate")
```
`pipeline.py`: delete `def reviewer(gql, cfg)` and its docstring/body.
- [ ] **Step 4:** full suite passes; `grep -n "reviewer" scripts/*.py` → nothing.
- [ ] **Step 5:** commit `TASK-69: promote subscribes the humans on In Review; drop pipeline.reviewer()`; push.

### Task 3: launch prompt drops `Reviewer:`

**Files:**
- Modify: `scripts/launch.py` (~143)
- Test: `scripts/tests/test_launch.py` (lines 196, 221-224, 229, 239, 279, 366, 496, 530)

- [ ] **Step 1: Tests.** Remove ` Reviewer: me@x.com.` from every expected string (keep ` Humans: …`); `expected()` loses `Reviewer: {humans[0]}. `; `test_memory_prompt_and_dir` expects `"… your role charter says how to use it. Humans: me@x.com."`; `test_real_config_resume_prompt` splits on `" Humans:"`; rename `test_reviewer_none` → `test_humans_none`, expecting `endswith(" Humans: none. Project: p-dr." + IDS)`.
- [ ] **Step 2:** `-k test_launch` → FAIL.
- [ ] **Step 3: Implement.**

```python
    tail = (f" Humans: {', '.join(humans) or 'none'}. Project: {a.project}."
            f" Team: {cfg['team']}. States: {states}.")
```
- [ ] **Step 4:** full suite passes; `grep -rn "Reviewer:" scripts` → nothing.
- [ ] **Step 5:** commit `TASK-69: launch prompt drops Reviewer:`; push.

### Task 4: rules and docs

**Files:** `roles/principles.md:7`, `tasks/engineering.md:20,39`, `pipeline.toml:2`, `README.md:14`, `CLAUDE.md:22` (+ one new Architecture bullet).

- [ ] **Step 1:** `roles/principles.md` line 7 becomes:
  `- Every move to In Review also subscribes each email in the prompt's `Humans:` list to the issue (`issueSubscribe(id:, userEmail:)`, one call per email) and leaves the assignee as is; if it is `none`, subscribe no one; if a subscribe fails, comment that and move anyway.`
- [ ] **Step 2:** `tasks/engineering.md`: step 2 `move it to In Review (reviewer assigned);` → `move it to In Review;`. Step 7 `Attach the PR URL to the issue; comment `Build ready:` + 3–5 lines incl. how to verify; assign the reviewer.` → `Attach the PR URL to the issue; subscribe the humans (principles); comment `Build ready:` + 3–5 lines incl. how to verify.`
- [ ] **Step 3:** `pipeline.toml` line 2 comment → `# the human users; subscribed to every issue moved to In Review`.
- [ ] **Step 4:** README line 14 → `- **Your turn:** the agent moves an issue to In Review and subscribes you when output is ready, it has questions, or it failed.`
- [ ] **Step 5:** CLAUDE.md: in the `launch.py` bullet drop `` `Reviewer:`, `` from the tail list; after the Identity bullet add `- Every move to In Review, by a run (`roles/principles.md`) or the harness (router's attempt cap, promote's bounce), also subscribes each `human_members` email (`issueSubscribe`) and keeps the assignee.`
- [ ] **Step 6:** `grep -rniE 'reviewer|assigneeId' roles tasks scripts README.md CLAUDE.md pipeline.toml` → only the claim's and Recover's `assigneeId` in `scripts/router.py`, their test fake, and `test_pipeline.py`'s `roles/Reviewer.*`. Full suite passes.
- [ ] **Step 7:** commit `TASK-69: rules and docs: subscribe the humans on In Review`; push.

## Verification (S6)

- `python3 -m unittest discover -s scripts/tests` → OK (baseline 267 tests OK at 909c4d3).
- The Step 6 grep of Task 4.
- `python3 scripts/router.py --now --dry-run` and `python3 scripts/promote.py --dry-run` are not run (they hit Linear); the unit fakes cover the calls.

## Progress
- decision(placement): subscribe inline in router `comment_and_move` and promote `move`, each in its file's idiom, over a shared pipeline helper - issue forbids new abstraction; dissent: none
- decision(order): subscribe before the state change - idempotent, a failure leaves the issue unmoved for retry; dissent: none
- decision(engineering step 7): replace "assign the reviewer" with "subscribe the humans" - the GitHub integration makes that move, so the principles rule would not fire; dissent: none
- S3 panel: core=[architecture,spec-fitness] +optional=[] transport=Workflow (security dropped: no auth/key/IO surface change)
- S3 r0: architecture=PASS spec-fitness=PASS -> converged; non-blockers applied: grep widened to scripts/, router comment non-idempotence noted
- decision(session subscribe failure): principles says comment and move anyway - mirrors the old unknown-email clause; the harness instead stays unmoved and retries next tick; dissent: none
