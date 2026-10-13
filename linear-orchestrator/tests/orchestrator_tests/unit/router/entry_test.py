import io
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from unittest import mock

from agent_pm.linear_orchestrator.engine import router
from agent_pm.linear_orchestrator.engine.routing import types as routing
from agent_pm.linear_orchestrator.linear import linear
from agent_pm.session import SessionStatus
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.board_ids import role as role_table
from orchestrator_tests.unit.clock import frozen
from orchestrator_tests.unit.outage import FAILURES, FIELDS, failing
from orchestrator_tests.unit.router.data import (
    LIGHT,
    NOW,
    OUTER,
    OVERLAY,
    SESSIONS,
    STRAY,
    ago,
    blocker,
    forwarded,
    inner,
    issue,
    label,
    other_router,
)
from orchestrator_tests.unit.router.fakes import ClaimLinear, FakeLinear, LostClaim
from orchestrator_tests.unit.router.fixtures import Base, OuterBase, TickBase
from orchestrator_tests.unit.run.support import HARNESS, ID, PROJECT, USER_NOTE, logged, node, show


class RouterEntry(OuterBase):
    """router.main end to end through its own outer (the old AttendedEntry's headless parts)."""

    def setUp(self):
        super().setUp()
        self.todo = issue(ID, "Todo", "engineer")
        self.todo["project"] = {"id": PROJECT, "name": "Agent PM"}
        self.fake = ClaimLinear(self.todo, node(comments=[USER_NOTE]))

    def entry(self, *argv, live=(), tty=False):
        outer = {"run": self.run_, "keychain": self.keychain}
        return self.tick(self.fake, *argv, live=live, again=True, tty=tty, outer=outer)

    def test_claims_then_starts_run_py_after_its_start_line(self):
        for argv, said in (
            (("--issue", ID), []),
            (("--now",), ["launch TASK-7 exit=0"]),
        ):
            with self.subTest(argv=argv):
                self.todo["state"], self.fake.mutations, self.api.detached = "Todo", [], []
                lines, detach = [], self.api.detach

                def at_detach(*args, lines=lines, detach=detach, **kw):
                    with open(self.runs) as f:
                        lines.append(f.read())
                    return detach(*args, **kw)

                with mock.patch.object(self.api, "detach", at_detach):
                    self.assertEqual(self.entry(*argv), 0)
                kind, ident, sid, role, runtime = self.last_run()
                self.assertEqual((kind, ident, role, runtime), ("start", ID, "engineer", "a"))
                self.assertEqual(lines, [self.state])
                self.assertEqual(
                    self.detached(), inner("--target", "Ophis/Agent-PM", *forwarded(sid=sid))
                )
                self.assertEqual(
                    self.said()[-2 - len(said) :],
                    ["pick TASK-7 queue=1", "claim TASK-7 role=engineer runtime=a", *said],
                )
                self.assertEqual(
                    self.fake.mutations,
                    [(linear.M_STATE, {"i": ID, "s": IDS_BY_KEY["in_progress"]})],
                )
                self.assertEqual(self.err, "")

    def test_a_task_label_reaches_run_py_as_its_task(self):
        self.write_config(OUTER + f'[task_labels]\nlight-build = "{LIGHT}"\n')
        self.todo["labels"] = [label("Light Build", LIGHT)]
        self.assertEqual(self.entry("--issue", ID), 0)
        sid = self.last_run()[2]
        self.assertEqual(
            self.detached(),
            inner("--target", "Ophis/Agent-PM", *forwarded(task="light-build", sid=sid)),
        )
        self.assertIn("claim TASK-7 role=engineer task=light-build runtime=a", self.said())

    def test_a_resume_keeps_the_runtime_of_its_claim(self):
        with frozen(ago(minutes=60)):
            self.assertEqual(self.entry(), 0)
        sid = self.last_run()[2]
        with open(self.runs) as f:
            self.lines = f.read().splitlines()
        self.api.statuses[sid] = SessionStatus("yes", (NOW - timedelta(minutes=40)).timestamp(), "")
        self.write_config(OUTER.replace(OVERLAY, '[core]\nruntime = "b"\n'))
        self.assertEqual(self.entry(), 0)
        self.assertEqual(self.last_run(), ("resume", ID, sid, "engineer", "a"))
        self.assertEqual(
            self.detached(),
            inner("--target", "Ophis/Agent-PM", *forwarded(sid=sid, mode="resume")),
        )
        self.assertEqual((self.api.info, self.api.usage_calls), ([("a", sid, self.rd)] * 2, ["a"]))

    def test_the_outers_lines_follow_the_ticks_plan(self):
        self.todo["state"], self.fake.node = "In Progress", None
        self.resumable(ID, "s", 60, role="engineer")
        self.assertEqual(self.entry(), 0)
        self.assertEqual(
            self.said(),
            [
                "plan TASK-7 mode=resume sid=s",
                "transient TASK-7 reason=Linear: IssueError: TASK-7: issue not found",
                "launch TASK-7 exit=3",
            ],
        )

    def test_a_live_run_is_refused(self):
        for role in ("engineer", "pm"):
            with self.subTest(role=role):
                self.assertEqual(self.entry("--issue", ID, live=[f"agent-pm-{role}-{ID}"]), 1)
                self.assertEqual(
                    self.err,
                    f"router.py: {ID} has a live agent run: attach agent-pm-{role}-{ID}\n",
                )
                self.assertEqual((self.api.order, self.state), (SESSIONS, ""))
        self.assertEqual((self.fake.queries, self.api.detached), ([], []))

    def test_nothing_started_exits_1_without_a_start_line(self):
        for change, said, state in (
            (
                {"state": "In Review"},
                "pick-none TASK-7 reason=not a Todo issue assigned to a role account",
                "In Review",
            ),
            (
                {"state": "In Progress"},
                f"recover TASK-7 to=todo reason=no session updated={self.todo['updatedAt']}",
                "Todo",
            ),
            (
                {"inverseRelations": {"nodes": [blocker("TASK-9")]}},
                'blocked TASK-7 by=["TASK-9"]',
                "Todo",
            ),
            (
                {"labels": [label("Stray", STRAY)]},
                "claim-skip TASK-7 to=in_review reason=bad task label",
                "In Review",
            ),
        ):
            with self.subTest(said=said):
                self.todo.update(state="Todo", inverseRelations={"nodes": []}, labels=[])
                self.todo.update(change)
                self.assertEqual(self.entry("--issue", ID), 1)
                self.assertIn(said, self.said())
                self.assertEqual(
                    (self.todo["state"], self.state, self.api.order), (state, "", SESSIONS)
                )
        self.assertEqual(self.api.detached, [])

    def test_a_config_error_exits_1_logged_once_a_day_and_printed_on_a_terminal(self):
        for text, reason in (
            ("bogus = 1\n" + OUTER, "linear-orchestrator/config.toml: unknown keys: bogus"),
            (
                OUTER + role_table("bogus"),
                "linear-orchestrator/config.toml: role 'bogus' is not in delegate/config.toml",
            ),
        ):
            self.write_config(text)
            self.logs = None
            for argv in (["--now"], ["--issue", ID]):
                for tty, err in ((False, ""), (True, f"router.py: {reason}\n")):
                    with self.subTest(reason=reason, argv=argv, tty=tty):
                        self.assertEqual(self.entry(*argv, tty=tty), 1)
                        self.assertEqual(self.err, err)
            self.assertEqual(
                [show(e) for e in logged(self.logs)], [f"config-error reason={reason}"]
            )
        self.logs = None
        self.write_local('work_dir = "w"\n')
        self.assertEqual(self.entry(), 1)
        self.assertEqual(
            (self.err, self.said()),
            (
                (
                    "linear-orchestrator/config.toml: work_dir must be a printable absolute"
                    " or ~ path: 'w'\n"
                ),
                [],
            ),
        )
        self.assertEqual((self.fake.queries, self.api.order, self.api.detached), ([], [], []))


class RouterArguments(Base):
    def main(self, argv):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = router.main(
                argv,
                config_path=os.path.join(self.tmp, "absent.toml"),
                services=router.RouterServices(gql=None),
            )
        return rc, err.getvalue()

    def test_unknown_flags_rejected(self):
        for argv in (
            ["--help"],
            ["-h"],
            ["--prune", "x"],
            ["--gate", "new"],
            ["--now", "--issue", "TASK-1"],
            ["--issue", "TASK-1", "--now"],
            ["--brake"],
            ["--brake", "--dry-run"],
            ["--brake", "--brake"],
            ["--dry-run", "--brake", "researcher"],
            ["--brake", "researcher", "--now"],
            ["--brake", "researcher", "pm"],
            ["--brake=researcher"],
            ["--issue", "TASK-1", "--brake", "researcher"],
            ["--tui"],
            ["--now", "--tui"],
            ["--issue", "TASK-1", "--tui"],
            ["--split", "right"],
            ["--split-from", "dev"],
            ["--events", "x"],
            ["--manager", "m1"],
            ["--now=x"],
            ["--issue=TASK-1"],
            ["--now", "--issue=TASK-1"],
            ["--issue", "TASK-1", "--issue", "TASK-2"],
            ["--issue"],
            ["--now", "--issue", "--dry-run"],
            *([f, "x"] for f in ("--uuid", "--target", "--project", "--assignee", "--sid")),
            *([f, "x"] for f in ("--task", "--mode", "--runtime", "--input", "--runner")),
            ["--issue", "TASK-1", "--runtime", "a"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(argv), (2, router.USAGE + "\n"))
        self.assertEqual((self.api.order, self.api.usage_calls), ([], []))
        self.assertEqual(
            router.USAGE,
            "usage: python3 -P -m agent_pm.linear_orchestrator.engine.router "
            "[--now] [--dry-run] [--issue ID] | --brake ROLE",
        )
        for flag in ("--gate", "--tui", "--split", "--events", "--manager"):
            self.assertNotIn(flag, router.USAGE + router.__doc__)

    def test_bad_issue_id(self):
        for argv in (
            ["--issue", "task-7"],
            ["--issue", "TASK"],
            ["--dry-run", "--issue", "TASK-7x"],
        ):
            with self.subTest(argv=argv):
                ident = argv[argv.index("--issue") + 1]
                self.assertEqual(self.main(argv), (2, f"router.py: bad issue id: {ident}\n"))


class RouterLocking(TickBase):
    def test_a_tick_skips_while_another_router_runs(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        before = self.before()
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with other_router(self.runs):
            self.assertEqual(self.tick(fake), 0)
            self.assertEqual(self.said(), ["skip reason=another router is running"])
            self.assertEqual(
                (fake.queries, self.api.order, self.state, fake.issues["TASK-1"]["state"]),
                ([], [], before, "Todo"),
            )
            self.assertEqual((self.tick(fake, hour=12), self.said(), self.api.order), (0, [], []))
        self.assertEqual(self.tick(fake), 0)
        self.assertEqual(self.launched_runs(), [("TASK-1", "new")])
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_another_router_leaves_the_issue_alone(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with other_router(self.runs):
            self.assertEqual(self.tick(fake, "--issue", "TASK-1"), 1)
        self.assertEqual(self.err, "router.py: another router is running; try again\n")
        self.assertEqual(self.said(), ["skip TASK-1 reason=another router is running"])
        self.assertEqual((fake.queries, self.api.order, self.state), ([], [], ""))

    def test_a_dry_run_and_the_brake_take_no_lock(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with other_router(self.runs):
            self.assertEqual(self.tick(fake, "--dry-run"), 0)
            self.assertEqual(self.said()[:-1], ["plan mode=new queue=1", "pick TASK-1 queue=1"])
            self.assertRegex(self.said()[-1], "^usage runtime=a ")
            self.assertEqual(self.tick(fake, "--dry-run", "--issue", "TASK-1"), 0)
            self.assertEqual(self.said(), ["pick TASK-1 queue=1"])
            out = io.StringIO()
            with redirect_stdout(out):
                rc = router.main(
                    ["--brake", "researcher"],
                    config_path=self.config_path,
                    services=router.RouterServices(driver=self.api),
                )
            self.assertEqual((rc, out.getvalue()), (0, "five_hour=10% weekly=10%\n"))
        self.assertEqual(fake.mutations, [])


class RouterOutages(TickBase):
    SAID = {name: f"linear-error op=teams {fields}" for name, fields in FIELDS.items()}

    def test_a_failure_at_board_setup_is_one_linear_error_a_day(self):
        for argv in (("--now",), ("--issue", "TASK-1")):
            for name, error in FAILURES.items():
                with self.subTest(argv=argv, failure=name):
                    for again in (False, True):
                        gql = failing(error)
                        self.assertEqual(self.tick(gql, *argv, again=again), 1)
                        self.assertEqual(
                            (self.said(), self.raw), ([] if again else [self.SAID[name]], "")
                        )
                        self.assertEqual(
                            gql.failed, [(linear.Q_TEAM, {"service": HARNESS, "t": TEAM})]
                        )
                        self.assertEqual((self.start.launches, self.state), ([], ""))
                    fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                    self.assertEqual(self.tick(fake, *argv), 0)
                    self.assertEqual(self.launched_runs(), [("TASK-1", "new")])

    def test_a_dry_run_prints_it_and_writes_nothing(self):
        for argv in (("--now", "--dry-run"), ("--issue", "TASK-1", "--dry-run")):
            for name, error in FAILURES.items():
                with self.subTest(argv=argv, failure=name):
                    self.assertEqual(self.tick(failing(error), *argv), 1)
                    self.assertEqual(
                        (self.said(), len(self.raw.splitlines())), ([self.SAID[name]], 1)
                    )
                    self.assertEqual((os.listdir(self.logs), self.state), (["runs.jsonl"], ""))

    def test_a_claim_applied_with_its_answer_lost_is_recovered_once_stale(self):
        for argv, plan in ((("--now",), ["plan mode=new queue=1"]), (("--issue", "TASK-1"), [])):
            with self.subTest(argv=argv):
                fake = LostClaim([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv), 1)
                self.assertEqual(
                    self.said(),
                    plan
                    + [
                        "pick TASK-1 queue=1",
                        "claim TASK-1 role=researcher runtime=a",
                        "linear-error op=issueUpdate reason=TimeoutError: timed out",
                    ],
                )
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], self.start.launches, self.state),
                    ("In Progress", [], ""),
                )
                self.assertEqual(
                    self.tick(fake, "--now", now=NOW + routing.STALE + timedelta(minutes=1)), 0
                )
                self.assertEqual(
                    self.said(),
                    [
                        f"recover TASK-1 to=todo reason=no session updated={ago(minutes=0)}",
                        "plan mode=new queue=1",
                        "pick TASK-1 queue=1",
                        "claim TASK-1 role=researcher runtime=a",
                        "launch TASK-1 exit=0",
                    ],
                )
                self.assertEqual(fake.issues["TASK-1"]["comments"], [routing.INTERRUPTED])
