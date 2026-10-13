import os
from datetime import timedelta

from agent_pm.linear_orchestrator.engine import router
from agent_pm.linear_orchestrator.engine.routing import types as routing
from agent_pm.session import ConfigError, DriverError, SessionStatus
from orchestrator_tests.unit.router.data import (
    CONFIG,
    EAST,
    HUMANS,
    IDS,
    LIGHT,
    NOW,
    OVERLAY,
    PD,
    ROLE,
    SESSIONS,
    TASK_LABELS,
    USER,
    IssueFacts,
    ago,
    issue,
    label,
    who,
)
from orchestrator_tests.unit.router.fakes import (
    Changed,
    FakeLinear,
    two,
)
from orchestrator_tests.unit.router.fixtures import (
    Attempt,
    Base,
    ClaimOptions,
)


class StaleRunRecovery(Base):
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
                fake = self.fake(
                    issue(
                        "TASK-1",
                        "In Progress",
                        "researcher",
                        facts=IssueFacts(updated=ago(hours=3)),
                    )
                )
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
                self.add(Attempt(kind="start", ident="TASK-1", sid="a", minutes_ago=minutes))
                self.api.statuses[self.sid("a")] = SessionStatus("no", None, "")
                self.assertEqual(self.plan(fake), out)
                t = fake.issues["TASK-1"]
                self.assertEqual(
                    (t["state"], t.get("comments")), (state, [routing.INTERRUPTED] if out else None)
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
                fake = self.fake(
                    issue(
                        "TASK-1",
                        "In Progress",
                        "researcher",
                        facts=IssueFacts(updated=ago(hours=3)),
                    )
                )
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
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", facts=IssueFacts(updated=ago(hours=3)))
        )
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


class ClaimRechecks(Base):
    """FR-56: the claim re-reads assignee and project; the claim is the recheck's."""

    def test_reassigned_to_no_role_account_is_not_claimed(self):
        for assignee in ({"id": USER, "email": "me@x.com"}, who("someone"), None):
            with self.subTest(assignee=assignee):
                fake = Changed(
                    [
                        issue("TASK-1", "Todo", "researcher", facts=IssueFacts(priority=1)),
                        issue("TASK-2", "Todo", "researcher", facts=IssueFacts(priority=2)),
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
            self.claim(fake, options=ClaimOptions(gate=True)),
            "TASK-1 https://linear.app/x/TASK-1 Deep Research",
        )
        taken, task, role, runtime = self.taken
        self.assertEqual((taken["assignee"], task, role, runtime), (who("pm"), None, "pm", "b"))
        self.assertEqual(self.said(), ["pick TASK-1 queue=1", "claim TASK-1 role=pm runtime=b"])
        self.assertEqual(self.api.usage_calls, ["b"])
        a = router.begin(("new", taken, "s1", role, task, runtime), self.runs)
        self.assertEqual((a.assignee, a.runtime), (ROLE["pm"], "b"))

    def test_the_task_label_is_checked_against_the_new_role(self):
        self.write_config(HUMANS + CONFIG + TASK_LABELS)
        fake = Changed(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    facts=IssueFacts(labels=[label("Light Research", LIGHT)]),
                )
            ],
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
        self.assertEqual(self.claim(fake, options=ClaimOptions(full={"pm"})), "")
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
        a = router.begin(("new", self.taken[0], "s1", "researcher", None, "a"), self.runs)
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


class LiveLimits(Base):
    """FR-60: a live limit row blocks its runtime with no probe; any other limit row changes
    nothing.
    """

    def test_a_live_limit_row_skips_its_runtime_without_a_probe(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        self.limit("a", at)
        fake = two()
        self.assertEqual(
            self.claim(fake, options=ClaimOptions(gate=True)),
            f"TASK-2 https://linear.app/x/TASK-2 {PD}",
        )
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=2",
                f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                "pick TASK-2 queue=2",
                "claim TASK-2 role=pm runtime=b",
            ],
        )
        self.assertEqual(self.api.usage_calls, ["b"])

    def test_a_limit_row_not_live_changes_nothing(self):
        rows = (
            (NOW - timedelta(minutes=1)).isoformat(),
            None,
            (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat(),
            5,
            "soon",
            (NOW + timedelta(days=8)).isoformat(),
        )
        for resets_at in rows:
            with self.subTest(resets_at=resets_at):
                self.lines, self.api.usage_calls = [], []
                self.limit("a", resets_at)
                fake = two()
                self.assertEqual(
                    self.claim(fake, options=ClaimOptions(gate=True)),
                    "TASK-1 https://linear.app/x/TASK-1 Deep Research",
                )
                self.assertEqual(self.api.usage_calls, ["a"])

    def test_limit_rows_are_no_attempts(self):
        for sid in "abc":
            self.add(Attempt(kind="start", ident="TASK-1", sid=sid, minutes_ago=300))
        for _ in range(4):
            self.limit("b", (NOW + timedelta(hours=1)).isoformat(), ident="TASK-1")
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.claim(fake), "TASK-1 https://linear.app/x/TASK-1 Deep Research")


class SessionInventory(Base):
    def test_exact_names_of_the_roles(self):
        self.api.live = [
            "agent-pm-engineer-TASK-2",
            "agent-pm-engineer-TASK-10",
            "agent-pm-researcher-TASK-1",
            "agent-pm-pm-TASK-6",
            "agent-pm-pm-lead-TASK-4",
            "agent-pm-engineer",
            "agent-pm-engineer-TASK-1x",
            "agent-pm-engineer-task-3",
            "agent-pm-engineer-TASK-",
            "agent-pm-ghost-TASK-5",
            "engineer-TASK-7-0b6f2c1e",
            "other",
        ]
        self.assertEqual(
            router.live_sessions(["researcher", "pm", "pm-lead", "engineer"], driver=self.api),
            {
                "researcher": ["TASK-1"],
                "pm": ["TASK-6"],
                "pm-lead": ["TASK-4"],
                "engineer": ["TASK-10", "TASK-2"],
            },
        )
        self.assertEqual(self.api.order, SESSIONS)

    def test_unknown_sessions_are_none(self):
        self.api.live = None
        self.assertIsNone(router.live_sessions(["researcher", "engineer"], driver=self.api))
