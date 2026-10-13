import io
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from unittest import mock

from agent_pm.linear_orchestrator.engine import config, router
from agent_pm.linear_orchestrator.linear import linear
from agent_pm.session import ConfigError, SessionStatus
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.board_ids import role as role_table
from orchestrator_tests.unit.clock import frozen
from orchestrator_tests.unit.router_data import (
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
    reading,
)
from orchestrator_tests.unit.router_fakes import ClaimLinear
from orchestrator_tests.unit.router_scenarios import Base, OuterBase
from orchestrator_tests.unit.run_fixtures import ID, PROJECT, USER_NOTE, logged, node, show


class Entry(OuterBase):
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


class Usage(Base):
    def main(self, argv):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = router.main(argv, gql=None, config_path=os.path.join(self.tmp, "absent.toml"))
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


class Brake(Base):
    """`--brake ROLE`: ROLE's runtime usage against BRAKE_5H and MAX_WEEK; no lock, no limit row."""

    def brake(self, role):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
        ):
            rc = router.main(["--brake", role], config_path=self.config_path, driver=self.api)
            self.path = os.environ["PATH"]
        self.err = err.getvalue()
        return rc, out.getvalue()

    def test_blocks_at_brake_5h(self):
        self.assertEqual(router.BRAKE_5H, 80)
        rows = (
            (reading(70), 0, "five_hour=70% weekly=10%"),
            (reading(79.9, 99.9), 0, "five_hour=79.9% weekly=99.9%"),
            (reading(80), 1, "five_hour=80% weekly=10%"),
            (reading(10, 100), 1, "five_hour=10% weekly=100%"),
            (reading(None, None), 0, "five_hour=none weekly=none"),
            (None, 1, "unknown"),
            (ConfigError("no runtime a"), 1, "unknown"),
            (RuntimeError("boom"), 1, "unknown"),
        )
        for value, rc, out in rows:
            with self.subTest(value=value):
                self.api.usages, self.api.usage_calls = {"a": value}, []
                self.assertEqual(self.brake("researcher"), (rc, out + "\n"))
                self.assertEqual((self.api.usage_calls, self.err), (["a"], ""))
        self.assertEqual(self.path, config.path())

    def test_the_roles_own_runtime(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        self.assertEqual(self.brake("pm"), (0, "five_hour=50% weekly=10%\n"))
        self.assertEqual(self.api.usage_calls, ["b"])

    def test_no_such_role(self):
        self.assertEqual(self.brake("ghost"), (2, ""))
        self.assertEqual((self.err, self.api.usage_calls), ("router.py: no role ghost\n", []))

    def test_a_live_limit_row_is_not_read(self):
        self.limit("a", (datetime.now(UTC) + timedelta(hours=1)).isoformat())
        self.write_runs()
        self.api.usages = {"a": reading(70)}
        self.assertEqual(self.brake("researcher"), (0, "five_hour=70% weekly=10%\n"))
        self.assertEqual(self.api.usage_calls, ["a"])
