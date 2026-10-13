import re

from agent_pm.linear_orchestrator.engine import router
from orchestrator_test_helpers.unit.router.blockers_fixtures import Blockers as BlockersBase
from orchestrator_test_helpers.unit.router.data import CONFIG, HUMANS, PD, blocker, issue
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear


class Blockers1(BlockersBase):
    def test_blocked_skipped_for_next_ready(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    priority=1,
                    inverse=[
                        blocker("TASK-7"),
                        blocker("TASK-8", "completed"),
                        blocker("TASK-9", "unstarted"),
                    ],
                ),
                issue("TASK-2", "Todo", "researcher", priority=2),
                issue("TASK-3", "Todo", "researcher", priority=3),
            ]
        )
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(
            self.said(),
            [
                'blocked TASK-1 by=["TASK-7", "TASK-9"]',
                "pick TASK-2 queue=2",
                "claim TASK-2 role=researcher runtime=a",
            ],
        )
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_done_blockers(self):
        for states, blocked in (
            (["completed"], False),
            (["canceled"], False),
            (["duplicate"], False),
            (["completed", "started"], True),
        ):
            fake = FakeLinear(
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        inverse=[blocker(f"TASK-{n}", s) for n, s in enumerate(states, 7)],
                    )
                ]
            )
            self.claim(fake)
            self.assertEqual(
                fake.issues["TASK-1"]["state"], "Todo" if blocked else "In Progress", states
            )
            self.assertEqual(
                [m for m in self.said() if m.startswith("blocked ")],
                ['blocked TASK-1 by=["TASK-8"]'] if blocked else [],
                states,
            )

    def test_capped_blocked_stays_todo(self):
        self.write_config(HUMANS + CONFIG)
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")])])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(
            self.said(), ['blocked TASK-1 by=["TASK-7"]', "pick-none reason=queue empty"]
        )
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("Todo", []))
        self.assertNotIn("comments", fake.issues["TASK-1"])

    def test_only_inverse_blocks_block(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    priority=2,
                    inverse=[
                        blocker("TASK-5", kind="related"),
                        blocker("TASK-6", kind="duplicate"),
                    ],
                ),
                issue(
                    "TASK-2",
                    "Todo",
                    "researcher",
                    priority=1,
                    inverse=[blocker("TASK-1", "unstarted")],
                ),
            ]
        )
        self.assertEqual(self.claim(fake).split()[0], "TASK-1")
        self.assertEqual(
            self.said(),
            [
                'blocked TASK-2 by=["TASK-1"]',
                "pick TASK-1 queue=1",
                "claim TASK-1 role=researcher runtime=a",
            ],
        )
        self.assertFalse(any(re.search(r"\brelations\b", q) for q, _ in fake.queries))

    def test_null_blocker_unreadable(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    inverse=[blocker(None), blocker("TASK-7", "completed")],
                )
            ]
        )
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(
            self.said(), ['blocked TASK-1 by=["(unreadable)"]', "pick-none reason=queue empty"]
        )

    def test_blocker_query_error_reads_per_issue(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "researcher", priority=2, inverse=[blocker("TASK-7")]),
                issue(
                    "TASK-3",
                    "Todo",
                    "researcher",
                    priority=3,
                    inverse=[blocker("TASK-8", "canceled")],
                ),
            ]
        )
        fake.todo_error, fake.unreadable = True, {"TASK-1"}
        self.assertEqual(self.claim(fake), "TASK-3 https://linear.app/x/TASK-3 Deep Research")
        said = self.said()
        self.assertRegex(said[0], r"^todo-error error=LinearError: linear api error: ")
        self.assertEqual(
            said[1:],
            [
                'blocked TASK-1 by=["(unreadable)"]',
                'blocked TASK-2 by=["TASK-7"]',
                "pick TASK-3 queue=1",
                "claim TASK-3 role=researcher runtime=a",
            ],
        )
        self.assertEqual(fake.reads(), [router.q_issues(router.RELATIONS), router.q_issues()])
        self.assertEqual(
            [v["i"] for q, v in fake.queries if "issue(id:" in q and "inverseRelations" in q],
            ["TASK-1", "TASK-2", "TASK-3"],
        )

    def test_one_todo_read_with_relations(self):
        for argv in ("claim", "plan", "pick"):
            fake = FakeLinear(
                [
                    issue("TASK-1", "In Progress", "researcher"),
                    issue("TASK-2", "Todo", "researcher"),
                ]
            )
            self.plan(fake) if argv == "plan" else self.claim(fake, recover=argv == "pick")
            reads = {s: fake.reads(s) for s in ("Todo", "In Progress")}
            self.assertEqual(len(reads["Todo"]), 1, argv)
            self.assertIn(
                "inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }",
                reads["Todo"][0],
                argv,
            )
            self.assertEqual(len(reads["In Progress"]), 0 if argv == "claim" else 1, argv)
            self.assertFalse(any("inverseRelations" in q for q in reads["In Progress"]), argv)
            self.assertFalse(
                any("issue(id:" in q and "inverseRelations" in q for q, _ in fake.queries), argv
            )

    def test_dry_run_logs_blocked(self):
        for argv, out, last in (
            ("claim", f"TASK-2 https://linear.app/x/TASK-2 {PD}", ["pick TASK-2 queue=1"]),
            ("plan", "new", []),
        ):
            fake = FakeLinear(
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        inverse=[blocker("TASK-7"), blocker("TASK-8", "backlog")],
                    ),
                    issue("TASK-2", "Todo", "pm", project=PD),
                ]
            )
            self.assertEqual(
                self.plan(fake, dry=True) if argv == "plan" else self.claim(fake, dry=True),
                out,
                argv,
            )
            self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7", "TASK-8"]', *last], argv)
            self.assertEqual(fake.mutations, [], argv)
