from datetime import timedelta

from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.unit.router.data import (
    CONFIG,
    DR,
    FULL,
    HUMANS,
    IDS,
    NOW,
    PD,
    RECHECK,
    ROLE,
    SESSIONS,
    ago,
    blocker,
    issue,
    outer_args,
    reading,
)
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear
from orchestrator_test_helpers.unit.router.issue_flag_fixtures import IssueFlag as IssueFlagBase
from orchestrator_test_helpers.unit.router.start_fixtures import Start


class IssueFlag1(IssueFlagBase):
    def test_todo_is_claimed_and_started_whatever_the_gates(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        self.limit("a", (NOW + timedelta(hours=1)).isoformat())
        self.api.usages = {"a": reading(95)}
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)),
            issue("TASK-2", "In Progress", "researcher", priority=1),
            issue("TASK-3", "Todo", "researcher", priority=1),
            issue("TASK-4", "Todo", "engineer", priority=3, project=PD),
        )
        self.resumable("TASK-2", "b", 60)
        self.assertEqual(self.tick(fake, "--issue", "TASK-4", hour=12, live=FULL), 0)
        self.assertEqual(
            self.said(), ["pick TASK-4 queue=1", "claim TASK-4 role=engineer runtime=a"]
        )
        (launch,) = self.start.launches
        self.assertEqual(
            launch, outer_args("TASK-4", IDS[PD], ROLE["engineer"], launch.sid, None, "new", "a")
        )
        self.assertRegex(launch.sid, config.UUID_RE)
        self.assertEqual(self.last_run(), ("start", "TASK-4", launch.sid, "engineer", "a"))
        self.assertIn("TASK-8", self.state)
        self.assertEqual((self.api.order, self.api.usage_calls), (SESSIONS, []))
        self.assertEqual(
            {i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
            {
                "TASK-1": ("In Progress", None),
                "TASK-2": ("In Progress", None),
                "TASK-3": ("Todo", None),
                "TASK-4": ("In Progress", None),
            },
        )
        self.assertEqual(len(fake.reads()), 1)

    def test_exits_with_the_outers_code(self):
        for rc in (0, 1, 3):
            with self.subTest(rc=rc):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                start = Start(exits={"TASK-1": rc})
                self.assertEqual(self.tick(fake, "--issue", "TASK-1", start=start), rc)

    def test_unknown_agent_runs_start_nothing(self):
        for dry in (False, True):
            with self.subTest(dry=dry):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                argv = ("--issue", "TASK-1", *["--dry-run"] * dry)
                self.assertEqual(self.tick(fake, *argv, live=None), 0 if dry else 1)
                self.assertEqual(self.err, "router.py: agent runs unknown; try again\n")
                self.assertEqual(
                    (fake.queries, self.start.launches, self.state, self.said()), ([], [], "", [])
                )

    def test_a_live_agent_run_is_refused_with_its_attach_command(self):
        for state in ("Todo", "In Progress", "In Review"):
            for dry in (False, True):
                with self.subTest(state=state, dry=dry):
                    fake = FakeLinear([issue("TASK-1", state, "engineer")])
                    argv = ("--issue", "TASK-1", *["--dry-run"] * dry)
                    self.assertEqual(
                        self.tick(fake, *argv, live=["agent-pm-engineer-TASK-1"]), 0 if dry else 1
                    )
                    self.assertEqual(
                        self.err,
                        "router.py: TASK-1 has a live agent run: attach agent-pm-engineer-TASK-1\n",
                    )
                    self.assertEqual(
                        (fake.queries, self.api.order, self.state, self.start.launches),
                        ([], SESSIONS, "", []),
                    )

    def test_in_progress_resumes_inside_recovers_time_windows(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.resumable("TASK-1", "a", 20)
        self.touch("a", 5)
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(self.tick(fake, "--issue", "TASK-1", hour=12), 0)
        self.assertEqual(self.said(), ["plan TASK-1 mode=resume sid=a"])
        (launch,) = self.start.launches
        self.assertEqual(
            launch,
            outer_args("TASK-1", IDS[DR], ROLE["researcher"], self.sid("a"), None, "resume", "a"),
        )
        self.assertEqual(self.last_run(), ("resume", "TASK-1", "a", "researcher", "a"))
        self.assertEqual((fake.mutations, self.api.usage_calls), ([], []))

    def test_in_progress_with_nothing_to_resume_is_settled_at_once(self):
        self.write_config(HUMANS + CONFIG)
        for name, found, given, said, state, comment in self.nothing_resumable():
            with self.subTest(name=name):
                self.lines, self.hist = [], {}
                given()
                fake = self.fake(found)
                before = self.before()
                if name in ("not resumable", "no sid"):
                    self.assertEqual((self.plan(fake), fake.mutations), ("", []))
                self.assertEqual(self.tick(fake, "--issue", "TASK-1"), 1)
                self.assertEqual(self.said(), [said])
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["comments"]),
                    (state, [comment]),
                )
                self.assertEqual(
                    (self.start.launches, self.api.usage_calls, self.state), ([], [], before)
                )

    def test_dry_run_prints_the_decision_only(self):
        def todo():
            return self.fake(issue("TASK-1", "Todo", "researcher"))

        def resumable():
            self.resumable("TASK-1", "a", 20)
            return self.fake(issue("TASK-1", "In Progress", "researcher"))

        cases = [
            ("todo", todo, ["pick TASK-1 queue=1"]),
            ("resume", resumable, ["plan TASK-1 mode=resume sid=a"]),
        ]
        cases += [
            (name, lambda found=found, given=given: (given(), self.fake(found))[1], [said])
            for name, found, given, said, _, _ in self.nothing_resumable()
        ]
        for name, given, said in cases:
            with self.subTest(name=name):
                self.lines, self.hist = [], {}
                fake = given()
                before = self.before()
                self.assertEqual(self.tick(fake, "--dry-run", "--issue", "TASK-1"), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], fake.mutations),
                    ("Todo" if name == "todo" else "In Progress", []),
                )
                self.assertEqual(
                    (self.start.launches, self.api.usage_calls, self.state), ([], [], before)
                )
                self.assertNotIn(RECHECK, [q for q, _ in fake.queries])

    def test_todo_not_claimable_starts_nothing(self):
        self.write_config(HUMANS + CONFIG)
        for argv in (("--issue", "TASK-1"), ("--issue", "TASK-1", "--dry-run")):
            with self.subTest(argv=argv):
                self.lines = []
                for sid in "abcd":
                    self.add("start", "TASK-1", sid, 300)
                before = self.before()
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv), 0 if "--dry-run" in argv else 1)
                self.assertEqual(
                    self.said(),
                    [
                        "claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                        "pick-none reason=nothing claimable",
                    ],
                )
                self.assertEqual(
                    fake.issues["TASK-1"]["state"], "Todo" if "--dry-run" in argv else "In Review"
                )
                self.assertEqual((self.start.launches, self.state), ([], before))

    def test_blocked_starts_nothing(self):
        for dry in (False, True):
            for resume in (False, True):
                with self.subTest(dry=dry, resume=resume):
                    self.lines, self.hist = [], {}
                    listed = [
                        issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")]),
                        issue("TASK-4", "Todo", "pm", project=PD),
                    ]
                    if resume:
                        listed.append(issue("TASK-2", "In Progress", "researcher"))
                        self.resumable("TASK-2", "a", 60)
                    fake = FakeLinear(listed, self.hist)
                    before = self.before()
                    self.assertEqual(
                        self.tick(fake, "--issue", "TASK-1", *["--dry-run"] * dry), 0 if dry else 1
                    )
                    self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7"]'])
                    self.assertEqual(
                        (self.api.usage_calls, self.start.launches, fake.mutations, self.state),
                        ([], [], [], before),
                    )
                    self.assertEqual(len(fake.reads()), 1)

    def test_another_state_or_not_a_role_account_starts_nothing(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "In Review", "researcher"),
                issue("TASK-2", "Todo", "someone"),
                issue("TASK-3", "Todo"),
                issue("TASK-4", "In Progress", "someone", updated=ago(hours=3)),
                issue("TASK-5", "Todo", "researcher", project=None),
            ]
        )
        for ident in ("TASK-1", "TASK-2", "TASK-3", "TASK-4", "TASK-5", "TASK-9"):
            with self.subTest(ident=ident):
                self.assertEqual(self.tick(fake, "--issue", ident), 1)
                self.assertEqual(
                    self.said(),
                    [f"pick-none {ident} reason=not a Todo issue assigned to a role account"],
                )
                self.assertEqual((self.start.launches, fake.mutations, self.state), ([], [], ""))
