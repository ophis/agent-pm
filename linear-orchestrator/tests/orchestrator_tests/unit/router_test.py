import json

from agent_pm.linear_orchestrator.engine import router
from agent_pm.linear_orchestrator.engine.types import LinearError
from orchestrator_tests.unit.router_data import (
    AGENT,
    CONFIG,
    DR,
    HUMANS,
    PD,
    STATES,
    USER,
    ago,
    issue,
    who,
    writes,
)
from orchestrator_tests.unit.router_fakes import FakeLinear, Moved
from orchestrator_tests.unit.router_scenarios import Base
from orchestrator_tests.unit.run_fixtures import HARNESS


class PlanNewOrNothingWithoutA(Base):
    def walk_fixture(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=2),
            issue("TASK-4", "In Progress", "researcher", priority=2),
            issue("TASK-3", "In Progress", "researcher", priority=3),
            issue("TASK-5", "In Progress", "researcher", priority=4),
            issue("TASK-6", "In Progress", "researcher", priority=4),
            issue("TASK-9", "Todo", "researcher", priority=1),
        )
        self.moved("TASK-1", 400)
        self.add("start", "TASK-1", "a", 399)
        self.resumable("TASK-3", "c", 100)
        self.resumable("TASK-6", "f", 50)
        for ident, sid in (("TASK-4", "d"), ("TASK-5", "e")):
            for m in (390, 380, 370, 360):
                self.add("start", ident, f"{sid}{m}", m)
        return fake

    def reset_fixture(self):
        self.write_config(HUMANS + CONFIG)
        self.moved("TASK-1", 700)
        self.add("start", "TASK-1", "old", 699)
        for m in (650, 600, 550):
            self.add("resume", "TASK-1", "old", m)
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.resumable("TASK-1", "new", 100)
        self.add("resume", "TASK-1", "new", 60)

    def capped(self, hist):
        self.write_config(HUMANS + CONFIG)
        fake = FakeLinear(
            [issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))], {"TASK-1": hist}
        )
        for i, sid in enumerate("abcd"):
            self.add("start", "TASK-1", sid, 400 - i * 50)
        self.touch("d", 200)
        return fake, self.plan(fake)

    def test_new_or_nothing_without_a_change(self):
        rows = (
            ("nothing", [issue("TASK-1", "In Review", "researcher")], ""),
            ("new", [issue("TASK-1", "Todo", "researcher")], "new"),
            (
                "other assignees not recovered",
                [
                    issue("TASK-1", "In Progress", "someone", updated=ago(hours=3)),
                    issue("TASK-2", "In Progress", None, updated=ago(hours=3)),
                ],
                "",
            ),
        )
        for name, issues_, out in rows:
            with self.subTest(name):
                fake = FakeLinear(issues_)
                self.assertEqual(self.plan(fake), out)
                self.assertEqual(fake.mutations, [])
                self.assertEqual(set(fake.services), {HARNESS})

    def test_attempt_cap_beats_resume(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=5)))
        self.resumable("TASK-1", "sid1", 300)
        for m in (200, 150, 100):
            self.add("resume", "TASK-1", "sid1", m)
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])

    def test_live_session_not_resumed_or_recovered(self):
        resume = "resume TASK-3 c https://linear.app/x/TASK-3 Deep Research"
        for others, out in (
            ((), ""),
            ((issue("TASK-2", "Todo", "researcher"),), "new"),
            ((issue("TASK-3", "In Progress", "researcher", priority=3),), resume),
        ):
            with self.subTest(out=out):
                self.lines, self.hist = [], {}
                fake = self.fake(
                    issue("TASK-1", "In Progress", "researcher", priority=1, updated=ago(hours=5)),
                    *others,
                )
                self.resumable("TASK-1", "a", 300)
                self.touch("a", 5)
                if any(i["state"] == "In Progress" for i in others):
                    self.resumable("TASK-3", "c", 100)
                self.assertEqual(self.plan(fake), out)
                self.assertEqual(fake.mutations, [])

    def test_live_session_issue_skipped_first_and_every_candidate_returned(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=1, updated=ago(hours=3)),
            issue("TASK-2", "In Progress", "researcher", priority=2, updated=ago(hours=3)),
            issue("TASK-3", "In Progress", "researcher", priority=3),
            issue("TASK-4", "In Progress", "pm", priority=4, project=PD),
        )
        for m in (390, 380, 370, 360):
            self.add("start", "TASK-1", f"a{m}", m)
        self.resumable("TASK-3", "c", 100)
        self.resumable("TASK-4", "d", 200, "pm")
        cands = self.board(fake, lambda b: b.recover({"TASK-1"}))
        self.assertEqual(
            [(i["identifier"], self.unmap(sid)) for i, sid in cands],
            [("TASK-3", "c"), ("TASK-4", "d")],
        )
        self.assertEqual(
            self.said(), [f"recover TASK-2 to=todo reason=no session updated={ago(hours=3)}"]
        )
        self.assertEqual(
            {i: t["state"] for i, t in fake.issues.items()},
            {
                "TASK-1": "In Progress",
                "TASK-2": "Todo",
                "TASK-3": "In Progress",
                "TASK-4": "In Progress",
            },
        )
        self.assertNotIn("comments", fake.issues["TASK-1"])
        self.assertNotIn("TASK-1", [v["i"] for q, v in fake.queries if "history" in q])
        self.assertEqual({self.unmap(sid) for _, sid, _ in self.api.info}, {"c", "d"})

    def test_resume_order(self):
        r = "researcher"
        rows = (
            ((("TASK-1", 3, 300, r, DR), ("TASK-2", 2, 100, r, DR)), DR),
            ((("TASK-1", 2, 100, r, DR), ("TASK-2", 2, 300, r, DR)), DR),
            ((("TASK-1", 0, 300, r, DR), ("TASK-2", 4, 100, r, DR)), DR),
            ((("TASK-1", 2, 120, r, DR), ("TASK-2", 2, 60, "pm", PD)), PD),
        )
        for specs, project in rows:
            with self.subTest(specs=specs):
                self.lines, self.hist = [], {}
                fake = self.fake(
                    *[
                        issue(i, "In Progress", role, priority=p, project=proj)
                        for i, p, _, role, proj in specs
                    ]
                )
                for ident, _, m, role, _ in specs:
                    self.resumable(ident, ident.lower(), m, role)
                self.assertEqual(
                    self.plan(fake), f"resume TASK-2 task-2 https://linear.app/x/TASK-2 {project}"
                )

    def test_no_current_sid_never_outranks(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=1),
            issue("TASK-2", "In Progress", "researcher", priority=4),
        )
        self.add("start", "TASK-1", "old", 400)
        self.touch("old", 300)
        self.moved("TASK-1", 60, actor=USER)
        self.resumable("TASK-2", "b", 100)
        self.assertEqual(
            self.plan(fake), "resume TASK-2 b https://linear.app/x/TASK-2 Deep Research"
        )

    def test_sid_without_start_sorts_by_first_line(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=2),
            issue("TASK-2", "In Progress", "researcher", priority=2),
        )
        self.add("resume", "TASK-1", "a", 300)
        self.add("resume", "TASK-1", "a", 50)
        self.touch("a", 40)
        self.resumable("TASK-2", "b", 200)
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )

    def test_walk_moves_before_and_after_candidate(self):
        resume = "resume TASK-3 c https://linear.app/x/TASK-3 Deep Research"
        for mode, out in (
            ("plan", resume),
            ("dry plan", resume),
            ("pick", "TASK-9 https://linear.app/x/TASK-9 Deep Research"),
        ):
            with self.subTest(mode=mode):
                self.lines, self.hist = [], {}
                fake = self.walk_fixture()
                self.assertEqual(
                    self.claim(fake, recover=True)
                    if mode == "pick"
                    else self.plan(fake, dry=mode == "dry plan"),
                    out,
                )
                self.assertIn("recover TASK-1 to=todo reason=not resumable sid=a", self.said())
                self.assertIn("recover TASK-5 to=in_review reason=reached 4 attempts", self.said())
                if mode == "dry plan":
                    self.assertEqual(fake.mutations, [])
                    continue
                states = {
                    i: fake.issues[i]["state"]
                    for i in ("TASK-1", "TASK-3", "TASK-4", "TASK-5", "TASK-6")
                }
                self.assertEqual(
                    states,
                    {
                        "TASK-1": "Todo",
                        "TASK-3": "In Progress",
                        "TASK-4": "In Review",
                        "TASK-5": "In Review",
                        "TASK-6": "In Progress",
                    },
                )
                self.assertEqual(fake.issues["TASK-1"]["comments"][0], router.INTERRUPTED)
                self.assertNotIn("comments", fake.issues["TASK-3"])

    def test_sid_without_transcript(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
        self.add("start", "TASK-1", "a", 10)
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.mutations, [])
        self.lines = []
        self.add("start", "TASK-1", "a", 40)
        self.assertEqual(self.plan(fake), "new")
        self.assertEqual(
            (fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]),
            ("Todo", who("researcher")),
        )

    def test_no_sid_keeps_two_hour_rule(self):
        self.write_config(HUMANS + CONFIG)
        fake = FakeLinear(
            [
                issue("TASK-1", "In Progress", "engineer", updated=ago(hours=3), project=PD),
                issue("TASK-2", "In Progress", "researcher", updated=ago(hours=1)),
            ]
        )
        self.plan(fake)
        t = fake.issues["TASK-1"]
        self.assertEqual(
            (t["state"], t["assignee"], t["comments"]),
            ("Todo", who("engineer"), [router.INTERRUPTED]),
        )
        self.assertNotIn("subscribers", t)
        self.assertFalse(any("assigneeId" in repr(v) for _, v in fake.mutations))
        self.assertEqual(fake.issues["TASK-2"]["state"], "In Progress")

    def test_stale_sid_goes_to_two_hour_rule(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=220)))
        self.add("start", "TASK-1", "old", 400)
        self.touch("old", 400)
        self.moved("TASK-1", 220, actor=USER)
        self.assertEqual(self.plan(fake), "new")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_tolerance(self):
        for start, want in ((100.05, "resume"), (106, "")):
            self.lines, self.hist = [], {}
            fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
            self.moved("TASK-1", 100)
            self.add("start", "TASK-1", "a", start)
            self.touch("a", 40)
            self.assertEqual(self.plan(fake).split(" ")[0], want)

    def test_no_move_in_history_is_current(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.add("start", "TASK-1", "a", 100)
        self.touch("a", 40)
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )

    def test_no_current_sid_never_capped(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=1)))
        for m in (600, 550, 500, 450):
            self.add("start", "TASK-1", f"s{m}", m)
        self.moved("TASK-1", 60, actor=USER)
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.mutations, [])

    def test_launch_failures_end_in_review(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        for n in range(4):
            self.moved("TASK-1", 41)
            self.add("start", "TASK-1", f"s{n}", 40)
            if n < 3:
                self.assertEqual(self.plan(fake), "new")
                self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")
                self.assertEqual(
                    self.claim(fake), "TASK-1 https://linear.app/x/TASK-1 Deep Research"
                )
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"][-1], router.CAP_COMMENT)

    def test_user_reset_resumes_and_orders_by_new_sid(self):
        rows = (
            ("resumes", (), "resume TASK-1 new https://linear.app/x/TASK-1 Deep Research"),
            (
                "orders",
                (issue("TASK-2", "In Progress", "researcher", priority=2),),
                "resume TASK-2 b https://linear.app/x/TASK-2 Deep Research",
            ),
        )
        for name, others, out in rows:
            with self.subTest(name):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=2), *others)
                self.reset_fixture()
                if others:
                    self.resumable("TASK-2", "b", 200)
                self.assertEqual(self.plan(fake), out)
                self.assertEqual(fake.mutations, [])

    def test_attempt_cap_subscribes_humans(self):
        for members in ([], ["me@x.com", "b@x.com"]):
            with self.subTest(members=members):
                self.write_config(
                    (f"human_members = {json.dumps(members)}\n" if members else "") + CONFIG
                )
                fake = FakeLinear(
                    [issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=40))]
                )
                self.lines = []
                for i, sid in enumerate("abcd"):
                    self.add("start", "TASK-1", sid, 400 - i * 50)
                self.assertEqual(self.plan(fake), "")
                t = fake.issues["TASK-1"]
                self.assertEqual(
                    (t["state"], t["assignee"], t["comments"], t.get("subscribers", [])),
                    ("In Review", who("researcher"), [router.CAP_COMMENT], members),
                )
                self.assertEqual(
                    writes(fake),
                    [("issueSubscribe", "TASK-1")] * len(members)
                    + [("read", "TASK-1"), ("issueUpdate", "TASK-1"), ("commentCreate", "TASK-1")],
                )
                self.assertEqual(fake.mutations[-2][1]["s"], STATES["In Review"])

    def test_unknown_human_fails_loud(self):
        self.write_config('human_members = ["nobody@x.com"]\n' + CONFIG)
        with self.assertRaises(LinearError) as e:
            self.plan(FakeLinear([]))
        self.assertIn("nobody@x.com", str(e.exception))

    def test_attempt_cap_reset_only_by_a_human_members_move_to_todo(self):
        def move(m, actor, state="Todo"):
            return {"createdAt": ago(minutes=m), "actorId": actor, "toStateId": STATES[state]}

        rows = (
            (
                "user",
                [move(380, USER)],
                "resume TASK-1 d https://linear.app/x/TASK-1 Deep Research",
                "In Progress",
            ),
            (
                "not a human member",
                [
                    move(380, AGENT),
                    move(375, "role-account"),
                    move(370, USER, "In Progress"),
                    move(360, None),
                ],
                "",
                "In Review",
            ),
        )
        for name, hist, out, state in rows:
            with self.subTest(name):
                self.lines = []
                fake, got = self.capped(hist)
                self.assertEqual((got, fake.issues["TASK-1"]["state"]), (out, state))
                if state == "In Progress":
                    self.assertEqual(fake.mutations, [])

    def test_skipped_move_posts_no_comment(self):
        fake = Moved(
            [issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))], "In Review"
        )
        self.assertEqual(self.plan(fake), "")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("In Review", []))
        self.assertNotIn("comments", fake.issues["TASK-1"])
        self.assertEqual(writes(fake), [("read", "TASK-1")])
        self.assertIn(f"move-skip TASK-1 step=recover state={STATES['In Review']}", self.said())


class PlanOneHistoryFetchPerIssue(Base):
    def test_one_history_fetch_per_issue(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        queries = []
        self.plan(lambda q, **v: queries.append(q) or fake(q, **v))
        self.assertEqual(sum("history" in q for q in queries), 1)
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
