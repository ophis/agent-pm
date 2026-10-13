from datetime import timedelta
from unittest import mock

from agent_pm.linear_orchestrator.linear import linear
from agent_pm.session import SessionStatus
from orchestrator_test_helpers.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_test_helpers.unit.board_ids import role as role_table
from orchestrator_test_helpers.unit.clock import frozen
from orchestrator_test_helpers.unit.router.data import (
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
    label,
)
from orchestrator_test_helpers.unit.router.entry_fixtures import Entry as EntryBase
from orchestrator_test_helpers.unit.run_fixtures import ID, logged, show


class Entry1(EntryBase):
    def test_claims_then_starts_run_py_after_its_start_line(self):
        for argv, said in (
            (("--issue", ID), []),
            (("--now",), ["launch TASK-7 exit=0"]),
        ):
            with self.subTest(argv=argv):
                self.todo["state"], self.fake.mutations, self.api.detached = "Todo", [], []
                lines, detach = [], self.api.detach

                def at_detach(*args, **kw):
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
                "linear-orchestrator/config.toml: work_dir must be a printable absolute or ~ path: 'w'\n",
                [],
            ),
        )
        self.assertEqual((self.fake.queries, self.api.order, self.api.detached), ([], [], []))
