import os
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

from agent_pm.linear_orchestrator.engine import promote
from agent_pm.linear_orchestrator.engine.prune import types as prune
from agent_pm.linear_orchestrator.engine.routing import types as routing
from agent_pm.linear_orchestrator.linear import issues, linear, sessions, writeback
from orchestrator_tests.case import Case
from orchestrator_tests.integration import fake_linear
from orchestrator_tests.unit.board_ids import STATES


class Table(Case):
    def test_every_query_constant_has_an_op(self):
        want = {
            f"{name}.{key}": value
            for name, module in (
                ("linear", linear),
                ("router", routing),
                ("issues", issues),
                ("sessions", sessions),
                ("writeback", writeback),
                ("promote", promote),
                ("prune", prune),
            )
            for key, value in vars(module).items()
            if key[:2] in ("Q_", "M_") and isinstance(value, str)
        }
        want.update(
            {
                "router.issues": routing.q_issues(),
                "router.issues+relations": routing.q_issues(routing.RELATIONS),
            }
        )
        self.assertEqual(fake_linear.texts(), want)

    def test_two_names_for_one_text_are_refused(self):
        with self.assertRaisesRegex(ValueError, "a.X, b.Y"):
            fake_linear.index({"a.X": "q", "b.Y": "q"})

    def test_fail_refuses_a_misuse(self):
        fake = fake_linear.FakeLinear()
        fake.user("engineer@agents.test", "Engineer", "linear-api-key-engineer")
        fake.user("me@x.com", "Me")
        for args, kw in (
            (("linear.M_NOPE", "error"), {}),
            (("linear.M_STATE", "500"), {}),
            (("linear.Q_ISSUE_STATE", "unsuccessful"), {}),
            (("linear.M_STATE", "error"), {"times": 0}),
            (("linear.M_STATE", "error"), {"account": "nobody@agents.test"}),
            (("linear.M_STATE", "error"), {"account": "me@x.com"}),
        ):
            with self.subTest(args, **kw), self.assertRaises(ValueError):
                fake.fail(*args, **kw)
        fake.fail("linear.M_STATE", "error", account="Engineer@agents.test")

    def test_seeding_refuses_a_used_or_malformed_issue_id(self):
        fake = fake_linear.FakeLinear()
        uid = fake.issue(identifier="TASK-7")["id"]
        for kw in ({"identifier": "TASK-7"}, {"id": uid}, {"identifier": "OTHER-1"}):
            with self.subTest(**kw), self.assertRaises(ValueError):
                fake.issue(**kw)

    def test_backlog_is_a_state_outside_the_config(self):
        self.assertNotIn(fake_linear.BACKLOG, STATES.values())
        self.assertEqual(
            (fake_linear.NAMES[fake_linear.BACKLOG], fake_linear.TYPES["backlog"]),
            ("backlog", "backlog"),
        )

    def test_a_backlog_issue_moves_to_todo_and_back(self):
        fake = fake_linear.FakeLinear()
        actor = fake.user("me@x.com", "Me")
        self.assertEqual(
            fake.issue("Later", identifier="TASK-1", state="backlog")["state"], fake_linear.BACKLOG
        )
        fake.move("TASK-1", "todo", actor=actor)
        self.assertEqual(fake.find("TASK-1")["state"], STATES["todo"])
        fake.move("TASK-1", "backlog", actor=actor)
        task = fake.find("TASK-1")
        self.assertEqual(task["state"], fake_linear.BACKLOG)
        self.assertEqual(
            [
                (fake_linear.NAMES[h["fromStateId"]], fake_linear.NAMES[h["toStateId"]])
                for h in task["history"]
            ],
            [("backlog", "todo"), ("todo", "backlog")],
        )

    def test_relate_records_the_issues_ids_and_refuses_an_unknown_one(self):
        fake = fake_linear.FakeLinear()
        a, b = fake.issue("A", identifier="TASK-1"), fake.issue("B", identifier="TASK-2")
        fake.relate("blocks", "TASK-1", b["id"])
        self.assertEqual(fake.relations, [("blocks", a["id"], b["id"])])
        for refs in (("TASK-1", "TASK-9"), ("TASK-9", "TASK-2"), (a["id"], "no-such-id")):
            with self.subTest(refs), self.assertRaises(ValueError):
                fake.relate("blocks", *refs)
        self.assertEqual(len(fake.relations), 1)

    def test_a_time_without_an_offset_is_utc(self):
        tz = mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"})
        self.addCleanup(time.tzset)
        tz.start()
        self.addCleanup(tz.stop)
        time.tzset()
        for t in (
            datetime(2026, 10, 9, 7, 30, 41, 62000),
            "2026-10-09T07:30:41.062",
            "2026-10-09T07:30:41.062Z",
            "2026-10-09T16:30:41.062+09:00",
            datetime(2026, 10, 9, 16, 30, 41, 62999, timezone(timedelta(hours=9))),
        ):
            with self.subTest(t):
                self.assertEqual(fake_linear.stamp(t), "2026-10-09T07:30:41.062Z")
