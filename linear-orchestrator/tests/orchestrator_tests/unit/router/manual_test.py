import argparse
import os
from datetime import timedelta
from unittest import mock

from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine.routing import types as routing
from agent_pm.linear_orchestrator.engine.types import LinearError
from orchestrator_tests.unit.outage import FAILURES, FIELDS, failing
from orchestrator_tests.unit.router.data import (
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
    IssueFacts,
    ago,
    blocker,
    issue,
    reading,
)
from orchestrator_tests.unit.router.fakes import FakeLinear, two
from orchestrator_tests.unit.router.fixtures import (
    Attempt,
    Start,
    TickBase,
    TickOptions,
    TickScenario,
)


class ExplicitIssueRuns(TickBase):
    def nothing_resumable(self):
        def capped():
            for m in (390, 380, 370, 360):
                self.add(Attempt(kind="start", ident="TASK-1", sid=f"s{m}", minutes_ago=m))

        return [
            (
                "cap",
                issue("TASK-1", "In Progress", "researcher"),
                capped,
                "recover TASK-1 to=in_review reason=reached 4 attempts",
                "In Review",
                routing.CAP_COMMENT,
            ),
            (
                "role",
                issue("TASK-1", "In Progress", "researcher"),
                lambda: self.resumable("TASK-1", "a", 60, "engineer"),
                (
                    "recover TASK-1 to=in_review reason=role (engineer) is not the "
                    "assignee's (researcher)"
                ),
                "In Review",
                (
                    "The interrupted agent run's role (engineer) is not the assignee's "
                    "(researcher); needs a look."
                ),
            ),
            (
                "not resumable",
                issue("TASK-1", "In Progress", "researcher"),
                lambda: self.add(Attempt(kind="start", ident="TASK-1", sid="n", minutes_ago=5)),
                "recover TASK-1 to=todo reason=not resumable sid=n",
                "Todo",
                routing.INTERRUPTED,
            ),
            (
                "no sid",
                issue(
                    "TASK-1", "In Progress", "researcher", facts=IssueFacts(updated=ago(minutes=5))
                ),
                lambda: None,
                f"recover TASK-1 to=todo reason=no session updated={ago(minutes=5)}",
                "Todo",
                routing.INTERRUPTED,
            ),
        ]

    def test_todo_is_claimed_and_started_whatever_the_gates(self):
        self.add(Attempt(kind="start", ident="TASK-8", sid="old", minutes_ago=60 * 24 * 8))
        self.limit("a", (NOW + timedelta(hours=1)).isoformat())
        self.api.usages = {"a": reading(95)}
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", facts=IssueFacts(updated=ago(hours=3))),
            issue("TASK-2", "In Progress", "researcher", facts=IssueFacts(priority=1)),
            issue("TASK-3", "Todo", "researcher", facts=IssueFacts(priority=1)),
            issue("TASK-4", "Todo", "engineer", facts=IssueFacts(priority=3, project=PD)),
        )
        self.resumable("TASK-2", "b", 60)
        self.assertEqual(
            self.tick(fake, "--issue", "TASK-4", options=TickOptions(hour=12, live=FULL)), 0
        )
        self.assertEqual(
            self.said(), ["pick TASK-4 queue=1", "claim TASK-4 role=engineer runtime=a"]
        )
        (launch,) = self.start.launches
        self.assertEqual(
            launch,
            argparse.Namespace(
                issue="TASK-4",
                project=IDS[PD],
                assignee=ROLE["engineer"],
                sid=launch.sid,
                task=None,
                mode="new",
                runtime="a",
            ),
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
                self.assertEqual(
                    self.tick(fake, "--issue", "TASK-1", options=TickOptions(start=start)), rc
                )

    def test_unknown_agent_runs_start_nothing(self):
        for dry in (False, True):
            with self.subTest(dry=dry):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                argv = ("--issue", "TASK-1", *["--dry-run"] * dry)
                self.assertEqual(
                    self.tick(fake, *argv, options=TickOptions(live=None)), 0 if dry else 1
                )
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
                        self.tick(
                            fake, *argv, options=TickOptions(live=["agent-pm-engineer-TASK-1"])
                        ),
                        0 if dry else 1,
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
        self.assertEqual(self.tick(fake, "--issue", "TASK-1", options=TickOptions(hour=12)), 0)
        self.assertEqual(self.said(), ["plan TASK-1 mode=resume sid=a"])
        (launch,) = self.start.launches
        self.assertEqual(
            launch,
            argparse.Namespace(
                issue="TASK-1",
                project=IDS[DR],
                assignee=ROLE["researcher"],
                sid=self.sid("a"),
                task=None,
                mode="resume",
                runtime="a",
            ),
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
                    self.add(Attempt(kind="start", ident="TASK-1", sid=sid, minutes_ago=300))
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
                        issue(
                            "TASK-1",
                            "Todo",
                            "researcher",
                            facts=IssueFacts(inverse=[blocker("TASK-7")]),
                        ),
                        issue("TASK-4", "Todo", "pm", facts=IssueFacts(project=PD)),
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
                issue("TASK-4", "In Progress", "someone", facts=IssueFacts(updated=ago(hours=3))),
                issue("TASK-5", "Todo", "researcher", facts=IssueFacts(project=None)),
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


class TickRecords(TickScenario):
    def test_a_usage_blocked_tick_writes_only_its_usage_skips_once_a_day(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", facts=IssueFacts(priority=1)),
                issue("TASK-2", "Todo", "engineer", facts=IssueFacts(priority=2, project=PD)),
            ]
        )
        for again, five, said in (
            (
                False,
                95,
                [
                    f"usage-skip TASK-{n} runtime=a reason=usage usage=five_hour=95% weekly=10%"
                    for n in (1, 2)
                ],
            ),
            (True, 97, []),
        ):
            with self.subTest(again=again):
                self.api.usages = {"a": reading(five)}
                self.assertEqual(self.tick(fake, options=TickOptions(again=again)), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual((self.start.launches, fake.mutations, self.state), ([], [], ""))

    def test_a_usage_skipped_head_then_a_claim_keeps_every_line(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        self.tick(two())
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=2",
                "pick TASK-1 queue=2",
                "usage-skip TASK-1 runtime=a reason=usage usage=five_hour=95% weekly=10%",
                "pick TASK-2 queue=2",
                "claim TASK-2 role=pm runtime=b",
                "launch TASK-2 exit=0",
            ],
        )

    def test_usage_skips_after_a_claim_skip_end_the_tick_quietly(self):
        self.api.usages = {"a": reading(95)}
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", facts=IssueFacts(priority=1)),
                issue("TASK-2", "Todo", "researcher", facts=IssueFacts(priority=2)),
            ]
        )
        for sid in "abcd":
            self.add(Attempt(kind="start", ident="TASK-1", sid=sid, minutes_ago=300))
        self.tick(fake)
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=2",
                "claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                "usage-skip TASK-2 runtime=a reason=usage usage=five_hour=95% weekly=10%",
            ],
        )

    def test_an_outage_mid_walk_writes_the_waiting_lines_first(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        gql = failing(FAILURES["timeout"], gql=fake, ops={"issue"})
        self.assertEqual(self.tick(gql), 1)
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=1",
                "pick TASK-1 queue=1",
                f"linear-error op=issue {FIELDS['timeout']}",
            ],
        )
        self.assertEqual((gql.failed[0][0], fake.mutations), (RECHECK, []))

    def test_trim_runs_before_plan(self):
        self.add(Attempt(kind="start", ident="TASK-8", sid="old", minutes_ago=60 * 24 * 8))
        self.tick(FakeLinear([]))
        self.assertNotIn("TASK-8", self.state)

    def test_trim_failure_continues(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.add(Attempt(kind="start", ident="TASK-8", sid="old", minutes_ago=60 * 24 * 8))
        with mock.patch.object(os, "replace", side_effect=OSError("disk")):
            self.tick(fake)
        self.assertIn("skip reason=prune failed error=OSError: disk", self.said())
        self.assertEqual(len(self.start.launches), 1)

    def test_the_outers_exit_and_exceptions_never_break_the_tick(self):
        cases = [
            (Start(exits={"TASK-1": 3}), "launch TASK-1 exit=3"),
            (
                Start(raises={"TASK-1": LinearError("linear api error: boom")}),
                "launch-error TASK-1 error=LinearError: linear api error: boom",
            ),
            (
                Start(raises={"TASK-1": RuntimeError("x")}),
                "launch-error TASK-1 error=RuntimeError: x",
            ),
        ]
        for start, end in cases:
            with self.subTest(end=end):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, options=TickOptions(start=start)), 0)
                self.assertEqual(self.said()[-1], end)
                (launch,) = self.start.launches
                self.assertEqual(
                    self.last_run(), ("start", "TASK-1", launch.sid, "researcher", "a")
                )

    def test_resume_reads_the_session_at_its_recorded_cwd(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.resumable("TASK-1", "a", 60)
        self.api.records[self.sid("a")] = {"cwd": self.tmp}
        self.tick(fake)
        self.assertEqual(self.launched_runs(), [("TASK-1", "resume")])
        self.assertEqual(self.api.info, [("a", self.sid("a"), self.tmp)])

    def test_driver_session_counts_toward_max_runs_and_a_tui_session_does_not(self):
        tui = config.tui_name("engineer", "TASK-1", self.sid("a"))
        for live, ran in (([tui, "agent-pm-engineer-TASK-9"], []), ([tui], [("TASK-1", "new")])):
            with self.subTest(live=live):
                self.tick(
                    FakeLinear([issue("TASK-1", "Todo", "engineer")]),
                    options=TickOptions(live=live),
                )
                self.assertEqual(self.launched_runs(), ran)
