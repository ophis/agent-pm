import re

from agent_pm.linear_orchestrator.engine.routing import types as routing
from agent_pm.linear_orchestrator.engine.types import LinearError
from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.router.data import (
    CONFIG,
    HUMANS,
    PD,
    ROLE,
    STATES,
    USER,
    IssueFacts,
    ago,
    blocker,
    issue,
    who,
    writes,
)
from orchestrator_tests.unit.router.fakes import FakeLinear, Moved
from orchestrator_tests.unit.router.fixtures import Attempt, Base, ClaimOptions


class IssueClaims(Base):
    def test_claim_order(self):
        rows = (
            (
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        facts=IssueFacts(priority=0, created="2026-01-01T00:00:00Z"),
                    ),
                    issue(
                        "TASK-2",
                        "Todo",
                        "researcher",
                        facts=IssueFacts(priority=3, created="2026-02-01T00:00:00Z"),
                    ),
                    issue(
                        "TASK-3",
                        "Todo",
                        "researcher",
                        facts=IssueFacts(priority=3, created="2026-01-15T00:00:00Z"),
                    ),
                ],
                ["TASK-3", "TASK-2", "TASK-1"],
            ),
            (
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        facts=IssueFacts(priority=2, created="2026-09-01T00:00:00Z"),
                    ),
                    issue(
                        "TASK-2",
                        "Todo",
                        "pm",
                        facts=IssueFacts(priority=2, created="2026-09-02T00:00:00Z", project=PD),
                    ),
                    issue(
                        "TASK-3",
                        "Todo",
                        "researcher",
                        facts=IssueFacts(priority=1, created="2026-09-03T00:00:00Z"),
                    ),
                ],
                ["TASK-3", "TASK-2", "TASK-1"],
            ),
            (
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        facts=IssueFacts(priority=2, created="2026-09-01T00:00:00Z"),
                    ),
                    issue(
                        "TASK-2",
                        "Todo",
                        "engineer",
                        facts=IssueFacts(priority=2, created="2026-09-03T00:00:00Z"),
                    ),
                    issue(
                        "TASK-3",
                        "Todo",
                        "pm",
                        facts=IssueFacts(priority=2, created="2026-09-02T00:00:00Z"),
                    ),
                    issue(
                        "TASK-4",
                        "Todo",
                        "engineer",
                        facts=IssueFacts(priority=2, created="2026-09-02T00:00:00Z"),
                    ),
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
                issue("TASK-3", "Todo", "pm", facts=IssueFacts(project=None)),
                issue("TASK-4", "Todo", "engineer", facts=IssueFacts(project=PD, priority=3)),
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
                issue("TASK-1", "Todo", "engineer", facts=IssueFacts(priority=1)),
                issue("TASK-2", "Todo", "researcher", facts=IssueFacts(priority=4)),
            ]
        )
        for sid in "abcd":
            self.add(Attempt(kind="start", ident="TASK-1", sid=sid, minutes_ago=300))
        self.assertEqual(
            self.claim(fake, options=ClaimOptions(full={"engineer"})),
            "TASK-2 https://linear.app/x/TASK-2 Deep Research",
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
                issue("TASK-1", "Todo", "researcher", facts=IssueFacts(priority=1)),
                issue("TASK-2", "Todo", "researcher", facts=IssueFacts(priority=2)),
            ]
        )
        for sid in "abcd":
            self.add(Attempt(kind="start", ident="TASK-1", sid=sid, minutes_ago=300))
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [routing.CAP_COMMENT])
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
            self.add(Attempt(kind="start", ident="TASK-1", sid=sid, minutes_ago=300))
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
                issue("TASK-1", "Todo", "researcher", facts=IssueFacts(priority=1)),
                issue("TASK-2", "Todo", "researcher", facts=IssueFacts(priority=2)),
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
            self.add(Attempt(kind="start", ident="TASK-1", sid=sid, minutes_ago=300))
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


class IssueBlockers(Base):
    def test_blocked_skipped_for_next_ready(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    facts=IssueFacts(
                        priority=1,
                        inverse=[
                            blocker("TASK-7"),
                            blocker("TASK-8", "completed"),
                            blocker("TASK-9", "unstarted"),
                        ],
                    ),
                ),
                issue("TASK-2", "Todo", "researcher", facts=IssueFacts(priority=2)),
                issue("TASK-3", "Todo", "researcher", facts=IssueFacts(priority=3)),
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
                        facts=IssueFacts(
                            inverse=[blocker(f"TASK-{n}", s) for n, s in enumerate(states, 7)]
                        ),
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
        fake = FakeLinear(
            [issue("TASK-1", "Todo", "researcher", facts=IssueFacts(inverse=[blocker("TASK-7")]))]
        )
        for sid in "abcd":
            self.add(Attempt(kind="start", ident="TASK-1", sid=sid, minutes_ago=300))
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
                    facts=IssueFacts(
                        priority=2,
                        inverse=[
                            blocker("TASK-5", kind="related"),
                            blocker("TASK-6", kind="duplicate"),
                        ],
                    ),
                ),
                issue(
                    "TASK-2",
                    "Todo",
                    "researcher",
                    facts=IssueFacts(priority=1, inverse=[blocker("TASK-1", "unstarted")]),
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
                    facts=IssueFacts(inverse=[blocker(None), blocker("TASK-7", "completed")]),
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
                issue("TASK-1", "Todo", "researcher", facts=IssueFacts(priority=1)),
                issue(
                    "TASK-2",
                    "Todo",
                    "researcher",
                    facts=IssueFacts(priority=2, inverse=[blocker("TASK-7")]),
                ),
                issue(
                    "TASK-3",
                    "Todo",
                    "researcher",
                    facts=IssueFacts(priority=3, inverse=[blocker("TASK-8", "canceled")]),
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
        self.assertEqual(fake.reads(), [routing.q_issues(routing.RELATIONS), routing.q_issues()])
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
            self.plan(fake) if argv == "plan" else self.claim(
                fake, options=ClaimOptions(recover=argv == "pick")
            )
            reads = {s: fake.reads(s) for s in ("Todo", "In Progress")}
            self.assertEqual(len(reads["Todo"]), 1, argv)
            self.assertIn(
                (
                    "inverseRelations(first: 50) { nodes { type issue { identifier state { "
                    "type } } } }"
                ),
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
                        facts=IssueFacts(inverse=[blocker("TASK-7"), blocker("TASK-8", "backlog")]),
                    ),
                    issue("TASK-2", "Todo", "pm", facts=IssueFacts(project=PD)),
                ]
            )
            self.assertEqual(
                self.plan(fake, dry=True)
                if argv == "plan"
                else self.claim(fake, options=ClaimOptions(dry=True)),
                out,
                argv,
            )
            self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7", "TASK-8"]', *last], argv)
            self.assertEqual(fake.mutations, [], argv)
