import os

from agent_pm.linear_orchestrator.engine import router
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.session import ConfigError, DriverError, SessionStatus
from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.router_data import (
    CONFIG,
    HUMANS,
    IDS,
    LIGHT,
    OVERLAY,
    PD,
    ROLE,
    STATES,
    TASK_LABELS,
    USER,
    ago,
    issue,
    label,
    who,
    writes,
)
from orchestrator_tests.unit.router_fakes import Changed, FakeLinear, Moved
from orchestrator_tests.unit.router_scenarios import Base


class Recover(Base):
    """FR-47: liveness and resumability are the Driver's session status; what it cannot say changes
    nothing.
    """

    def test_a_session_active_within_live_is_not_resumed(self):
        for minutes, out in (
            (10, ""),
            (29, ""),
            (31, "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"),
        ):
            with self.subTest(minutes=minutes):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
                self.resumable("TASK-1", "a", 60)
                self.touch("a", minutes)
                self.assertEqual(self.plan(fake), out)
                self.assertEqual((fake.mutations, self.said()), ([], []))

    def test_not_resumable_goes_to_todo_after_live(self):
        for minutes, out, state in ((20, "", "In Progress"), (40, "new", "Todo")):
            with self.subTest(minutes=minutes):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
                self.moved("TASK-1", minutes + 1)
                self.add("start", "TASK-1", "a", minutes)
                self.api.statuses[self.sid("a")] = SessionStatus("no", None, "")
                self.assertEqual(self.plan(fake), out)
                t = fake.issues["TASK-1"]
                self.assertEqual(
                    (t["state"], t.get("comments")), (state, [router.INTERRUPTED] if out else None)
                )
                self.assertEqual(
                    self.said(),
                    ["recover TASK-1 to=todo reason=not resumable sid=a"] if out else [],
                )

    def test_an_unknown_session_changes_nothing_and_is_said_once_a_day(self):
        statuses = (
            SessionStatus("unknown", None, ""),
            DriverError("boom"),
            ConfigError("no runtime a"),
            OSError("gone"),
        )
        for st in statuses:
            with self.subTest(st=st):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
                self.resumable("TASK-1", "a", 300)
                self.api.statuses[self.sid("a")] = st
                for again, said in (
                    (False, ["recover-skip TASK-1 sid=a reason=session unknown"]),
                    (True, []),
                ):
                    self.assertEqual(self.plan(fake, again=again), "")
                    self.assertEqual(self.said(), said)
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], fake.mutations), ("In Progress", [])
                )
                with open(self.runs) as f:
                    self.assertEqual(f.read(), "".join(f"{line}\n" for line in self.lines))

    def test_an_unknown_session_is_not_settled_without_the_wait(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
        self.resumable("TASK-1", "a", 300)
        self.api.statuses[self.sid("a")] = DriverError("boom")

        def use(b):
            (found,) = b.issues("in_progress")
            return b.settle(found, b.current_sid(found), wait=False)

        self.assertFalse(self.board(fake, use))
        self.assertEqual(self.said(), ["recover-skip TASK-1 sid=a reason=session unknown"])
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("In Progress", []))

    def test_one_status_read_per_sid_with_its_recorded_runtime_at_the_workdir(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.resumable("TASK-1", "a", 60)
        self.write_config(CONFIG.replace(OVERLAY, '[core]\nruntime = "b"\n'))
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )
        self.assertEqual(
            self.api.info, [("a", self.sid("a"), os.path.join(self.work, "work", "TASK-1"))]
        )


class Claim(Base):
    def test_claim_order(self):
        rows = (
            (
                [
                    issue(
                        "TASK-1", "Todo", "researcher", priority=0, created="2026-01-01T00:00:00Z"
                    ),
                    issue(
                        "TASK-2", "Todo", "researcher", priority=3, created="2026-02-01T00:00:00Z"
                    ),
                    issue(
                        "TASK-3", "Todo", "researcher", priority=3, created="2026-01-15T00:00:00Z"
                    ),
                ],
                ["TASK-3", "TASK-2", "TASK-1"],
            ),
            (
                [
                    issue(
                        "TASK-1", "Todo", "researcher", priority=2, created="2026-09-01T00:00:00Z"
                    ),
                    issue(
                        "TASK-2",
                        "Todo",
                        "pm",
                        priority=2,
                        created="2026-09-02T00:00:00Z",
                        project=PD,
                    ),
                    issue(
                        "TASK-3", "Todo", "researcher", priority=1, created="2026-09-03T00:00:00Z"
                    ),
                ],
                ["TASK-3", "TASK-2", "TASK-1"],
            ),
            (
                [
                    issue(
                        "TASK-1", "Todo", "researcher", priority=2, created="2026-09-01T00:00:00Z"
                    ),
                    issue("TASK-2", "Todo", "engineer", priority=2, created="2026-09-03T00:00:00Z"),
                    issue("TASK-3", "Todo", "pm", priority=2, created="2026-09-02T00:00:00Z"),
                    issue("TASK-4", "Todo", "engineer", priority=2, created="2026-09-02T00:00:00Z"),
                ],
                ["TASK-4", "TASK-2", "TASK-3", "TASK-1"],
            ),
        )
        for row, (listed, order) in enumerate(rows):
            with self.subTest(row=row):
                fake = FakeLinear(listed)
                self.assertEqual([self.claim(fake).split()[0] for _ in listed], order)
                self.assertEqual({i["state"] for i in fake.issues.values()}, {"In Progress"})

    def test_queue_only_role_accounts_across_projects(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo"),
                issue("TASK-2", "Todo", "someone"),
                issue("TASK-3", "Todo", "pm", project=None),
                issue("TASK-4", "Todo", "engineer", project=PD, priority=3),
            ]
        )
        self.assertEqual(self.claim(fake), f"TASK-4 https://linear.app/x/TASK-4 {PD}")
        self.assertEqual(
            [i for i in ("TASK-1", "TASK-2", "TASK-3") if fake.issues[i]["state"] != "Todo"], []
        )
        flt = next(v["f"] for q, v in fake.queries if "issues(filter" in q)
        self.assertEqual(
            (flt["team"], flt["project"], sorted(flt["assignee"]["id"]["in"])),
            ({"id": {"eq": TEAM}}, {"null": False}, sorted(f"u-{r}" for r in ROLE)),
        )

    def test_claim_only_moves_to_in_progress(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "pm")])
        self.claim(fake)
        ((q, v),) = fake.mutations
        self.assertNotIn("assignee", q + repr(v))
        self.assertEqual(
            (fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]),
            ("In Progress", who("pm")),
        )
        self.assertEqual(self.taken[1:], (None, "pm", "b"))

    def test_full_roles_not_picked(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "engineer", priority=1),
                issue("TASK-2", "Todo", "researcher", priority=4),
            ]
        )
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(
            self.claim(fake, full={"engineer"}), "TASK-2 https://linear.app/x/TASK-2 Deep Research"
        )
        self.assertEqual(
            self.said(), ["pick TASK-2 queue=1", "claim TASK-2 role=researcher runtime=a"]
        )
        self.assertEqual(
            (fake.issues["TASK-1"]["state"], "comments" in fake.issues["TASK-1"]), ("Todo", False)
        )
        flt = next(v["f"] for q, v in fake.queries if "issues(filter" in q)
        self.assertEqual(sorted(flt["assignee"]["id"]["in"]), sorted(f"u-{r}" for r in ROLE))

    def test_capped_todo_goes_to_review(self):
        self.write_config(HUMANS + CONFIG)
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "researcher", priority=2),
            ]
        )
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])
        self.assertEqual(
            (fake.issues["TASK-1"]["assignee"], fake.issues["TASK-1"]["subscribers"]),
            (who("researcher"), ["me@x.com"]),
        )

    def test_capped_todo_reset_by_user(self):
        self.write_config(HUMANS + CONFIG)
        hist = {
            "TASK-1": [
                {"createdAt": ago(minutes=200), "actorId": USER, "toStateId": STATES["Todo"]}
            ]
        }
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")], hist)
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "TASK-1 https://linear.app/x/TASK-1 Deep Research")

    def test_no_longer_todo_skipped_for_the_next(self):
        class Snatched(FakeLinear):
            def __call__(self, query, **v):
                out = super().__call__(query, **v)
                if "issues(filter" in query and "inverseRelations" in query:
                    self.issues["TASK-1"]["state"] = "In Progress"
                return out

        fake = Snatched(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "researcher", priority=2),
            ]
        )
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=2",
                "claim-skip TASK-1 reason=no longer Todo",
                "pick TASK-2 queue=2",
                "claim TASK-2 role=researcher runtime=a",
            ],
        )
        self.assertEqual([v["i"] for _, v in fake.mutations], ["TASK-2"])

    def test_skipped_cap_move_posts_no_comment(self):
        self.write_config(HUMANS + CONFIG)
        fake = Moved([issue("TASK-1", "Todo", "researcher")], "In Progress")
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "")
        t = fake.issues["TASK-1"]
        self.assertEqual((t["state"], t["subscribers"]), ("In Progress", ["me@x.com"]))
        self.assertNotIn("comments", t)
        self.assertEqual(writes(fake), [("issueSubscribe", "TASK-1"), ("read", "TASK-1")])
        self.assertEqual(
            self.said(),
            [
                "claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                f"move-skip TASK-1 step=pick state={STATES['In Progress']}",
                "pick-none reason=nothing claimable",
            ],
        )

    def test_refused_claim_raises(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        fake.refuse.add("TASK-1")
        with self.assertRaises(LinearError) as cm:
            self.claim(fake)
        self.assertEqual(str(cm.exception), "issueUpdate: success: false")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")


class Recheck(Base):
    """FR-56: the claim re-reads assignee and project; the claim is the recheck's."""

    def test_reassigned_to_no_role_account_is_not_claimed(self):
        for assignee in ({"id": USER, "email": "me@x.com"}, who("someone"), None):
            with self.subTest(assignee=assignee):
                fake = Changed(
                    [
                        issue("TASK-1", "Todo", "researcher", priority=1),
                        issue("TASK-2", "Todo", "researcher", priority=2),
                    ],
                    {"TASK-1": {"assignee": assignee}},
                )
                self.assertEqual(
                    self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research"
                )
                self.assertEqual(
                    self.said(),
                    [
                        "pick TASK-1 queue=2",
                        "claim-skip TASK-1 reason=not assigned to a role",
                        "pick TASK-2 queue=2",
                        "claim TASK-2 role=researcher runtime=a",
                    ],
                )
                self.assertEqual([v["i"] for _, v in fake.mutations], ["TASK-2"])

    def test_reassigned_to_another_role_is_claimed_as_that_role(self):
        fake = Changed([issue("TASK-1", "Todo", "researcher")], {"TASK-1": {"assignee": who("pm")}})
        self.assertEqual(
            self.claim(fake, gate=True), "TASK-1 https://linear.app/x/TASK-1 Deep Research"
        )
        taken, task, role, runtime = self.taken
        self.assertEqual((taken["assignee"], task, role, runtime), (who("pm"), None, "pm", "b"))
        self.assertEqual(self.said(), ["pick TASK-1 queue=1", "claim TASK-1 role=pm runtime=b"])
        self.assertEqual(self.api.usage_calls, ["b"])
        a = router.begin("new", taken, "s1", role, task, runtime, self.runs)
        self.assertEqual((a.assignee, a.runtime), (ROLE["pm"], "b"))

    def test_the_task_label_is_checked_against_the_new_role(self):
        self.write_config(HUMANS + CONFIG + TASK_LABELS)
        fake = Changed(
            [issue("TASK-1", "Todo", "researcher", labels=[label("Light Research", LIGHT)])],
            {"TASK-1": {"assignee": who("pm")}},
        )
        self.assertEqual(self.claim(fake), "")
        t = fake.issues["TASK-1"]
        self.assertEqual(
            (t["state"], t["comments"]),
            (
                "In Review",
                [
                    'Task label "Light Research" is not one of pm\'s tasks (product-design). '
                    "Fix the label or the assignee, then move the issue back to Todo."
                ],
            ),
        )

    def test_a_full_new_role_is_skipped(self):
        fake = Changed([issue("TASK-1", "Todo", "researcher")], {"TASK-1": {"assignee": who("pm")}})
        self.assertEqual(self.claim(fake, full={"pm"}), "")
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=1",
                "claim-skip TASK-1 reason=pm is full",
                "pick-none reason=nothing claimable",
            ],
        )
        self.assertEqual((fake.mutations, fake.issues["TASK-1"]["state"]), ([], "Todo"))

    def test_a_changed_project_is_the_runs(self):
        fake = Changed(
            [issue("TASK-1", "Todo", "researcher")],
            {"TASK-1": {"project": {"id": IDS[PD], "name": PD}}},
        )
        self.assertEqual(self.claim(fake), f"TASK-1 https://linear.app/x/TASK-1 {PD}")
        a = router.begin("new", self.taken[0], "s1", "researcher", None, "a", self.runs)
        self.assertEqual(a.project, IDS[PD])

    def test_a_removed_project_is_skipped(self):
        fake = Changed([issue("TASK-1", "Todo", "researcher")], {"TASK-1": {"project": None}})
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=1",
                "claim-skip TASK-1 reason=no project",
                "pick-none reason=nothing claimable",
            ],
        )
        self.assertEqual((fake.mutations, fake.issues["TASK-1"]["state"]), ([], "Todo"))
