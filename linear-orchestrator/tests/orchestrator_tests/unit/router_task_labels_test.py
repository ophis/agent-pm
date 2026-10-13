import io
from contextlib import redirect_stderr
from datetime import timedelta

from agent_pm.linear_orchestrator.engine import router
from agent_pm.session import ConfigError
from orchestrator_tests.unit.board_ids import TASK_GROUP
from orchestrator_tests.unit.router_data import (
    DEEP,
    DR,
    EAST,
    LIGHT,
    NOW,
    ORPHAN_LABEL,
    PD,
    RECHECK,
    STATES,
    STRAY,
    blocker,
    issue,
    label,
    reading,
    who,
    writes,
)
from orchestrator_tests.unit.router_fakes import Changed, FakeLinear, two
from orchestrator_tests.unit.router_scenarios import Base, TaskLabelsScenario


class TaskLabels(TaskLabelsScenario):
    def test_task_for(self):
        outside = [
            label("Urgent", STRAY, None),
            label("Light Research", LIGHT, "00000000-0000-4000-8000-000000000003"),
        ]
        role_tasks = ["deep-research", "light-research"]
        label_tasks = {
            LIGHT: "light-research",
            DEEP: "deep-research",
            ORPHAN_LABEL: "product-design",
        }
        fix = "Fix the label or the assignee, then move the issue back to Todo."
        absent = (
            "is not in ~/.agent-pm/config.local.toml's [task_labels]. Fix the "
            "label, then move the issue back to Todo."
        )
        cases = (
            ([], (None, None)),
            (outside, (None, None)),
            ([label("Light Research", LIGHT)], ("light-research", None)),
            ([label("Renamed in Linear", LIGHT)], ("light-research", None)),
            (outside + [label("Light Research", LIGHT)], ("light-research", None)),
            ([label("Whatever", DEEP)], ("deep-research", None)),
            ([label("Light Research", STRAY)], (None, f'Task label "Light Research" {absent}')),
            ([label("light-research", STRAY)], (None, f'Task label "light-research" {absent}')),
            (
                [label("Orphan", ORPHAN_LABEL)],
                (
                    None,
                    (
                        f"""Task label "Orphan" is not one of researcher's tasks (deep-research, """
                        f"light-research). {fix}"
                    ),
                ),
            ),
            (
                [label("Light Research", LIGHT), outside[0], label("Deep Research", DEEP)],
                (
                    None,
                    (
                        "Several task labels (Light Research, Deep Research); keep one, then "
                        "move the issue back to Todo."
                    ),
                ),
            ),
        )
        for labels, want in cases:
            self.assertEqual(
                router.task_for(labels, TASK_GROUP, "researcher", role_tasks, label_tasks),
                want,
                labels,
            )

    def test_fr7_invalid_label_goes_to_review(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    priority=1,
                    labels=[label("Orphan", ORPHAN_LABEL)],
                ),
                issue("TASK-2", "Todo", "researcher", priority=2),
            ]
        )
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        t = fake.issues["TASK-1"]
        self.assertEqual(
            (t["state"], t["assignee"], t["comments"], t["subscribers"]),
            ("In Review", who("researcher"), [self.ORPHAN], ["me@x.com"]),
        )
        self.assertEqual(
            writes(fake),
            [
                ("issueSubscribe", "TASK-1"),
                ("read", "TASK-1"),
                ("issueUpdate", "TASK-1"),
                ("commentCreate", "TASK-1"),
                ("issueUpdate", "TASK-2"),
            ],
        )
        self.assertEqual(fake.mutations[1][1]["s"], STATES["In Review"])
        self.assertIn("claim-skip TASK-1 to=in_review reason=bad task label", self.said())

    def test_capped_and_bad_label_todo_skipped_for_the_next(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue(
                    "TASK-2",
                    "Todo",
                    "researcher",
                    priority=2,
                    labels=[label("Orphan", ORPHAN_LABEL)],
                ),
                issue("TASK-3", "Todo", "pm", priority=3, project=PD),
                issue("TASK-4", "Todo", "engineer", priority=4, project=PD),
            ]
        )
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), f"TASK-3 https://linear.app/x/TASK-3 {PD}")
        self.assertEqual(
            self.said(),
            [
                "claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                "pick TASK-2 queue=4",
                "claim-skip TASK-2 to=in_review reason=bad task label",
                "pick TASK-3 queue=4",
                "claim TASK-3 role=pm runtime=b",
            ],
        )
        self.assertEqual(
            {i: fake.issues[i].get("comments") for i in ("TASK-1", "TASK-2")},
            {"TASK-1": [router.CAP_COMMENT], "TASK-2": [self.ORPHAN]},
        )
        self.assertEqual([i for op, i in writes(fake) if op == "read"], ["TASK-1", "TASK-2"])
        self.assertEqual([v["i"] for q, v in fake.queries if q == RECHECK], ["TASK-2", "TASK-3"])
        self.assertEqual(fake.issues["TASK-4"]["state"], "Todo")

    def test_blocked_invalid_label_waits_for_its_blocker(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    labels=[label("Orphan", ORPHAN_LABEL)],
                    inverse=[blocker("TASK-7")],
                )
            ]
        )
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(
            self.said(), ['blocked TASK-1 by=["TASK-7"]', "pick-none reason=queue empty"]
        )
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("Todo", []))
        self.assertNotIn(RECHECK, [q for q, _ in fake.queries])
        fake.issues["TASK-1"]["inverseRelations"]["nodes"][0]["issue"]["state"]["type"] = (
            "completed"
        )
        self.assertEqual(self.claim(fake), "")
        t = fake.issues["TASK-1"]
        self.assertEqual((t["state"], t["comments"]), ("In Review", [self.ORPHAN]))
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=1",
                "claim-skip TASK-1 to=in_review reason=bad task label",
                "pick-none reason=nothing claimable",
            ],
        )

    def test_fr4_label_read_from_recheck(self):
        class Relabel(FakeLinear):
            def __call__(self, query, **v):
                out = super().__call__(query, **v)
                if "issues(filter" in query:
                    self.issues["TASK-1"]["labels"] = [label("Light Research", LIGHT)]
                return out

        fake = Relabel([issue("TASK-1", "Todo", "researcher")])
        self.claim(fake)
        self.assertIn("claim TASK-1 role=researcher task=light-research runtime=a", self.said())
        self.assertEqual(self.taken[1:], ("light-research", "researcher", "a"))
        self.assertNotIn("labels", fake.reads()[0])

    def test_fr8_dry_run_resolves_nothing(self):
        for argv in (False, True):
            fake = FakeLinear(
                [issue("TASK-1", "Todo", "researcher", labels=[label("Orphan", ORPHAN_LABEL)])]
            )
            self.assertEqual(
                self.claim(fake, dry=True, recover=argv),
                "TASK-1 https://linear.app/x/TASK-1 Deep Research",
                argv,
            )
            self.assertEqual(self.said(), ["pick TASK-1 queue=1"], argv)
            self.assertEqual(
                ([q for q, _ in fake.queries if q == RECHECK], fake.mutations), ([], []), argv
            )
            self.assertEqual(self.taken[1:], (None, "researcher", "a"))

    def test_nfr1_claim_reads_one_query_per_issue(self):
        fake = FakeLinear(
            [issue("TASK-1", "Todo", "researcher", labels=[label("Light Research", LIGHT)])]
        )
        self.claim(fake)
        self.assertEqual(
            [(q, v) for q, v in fake.queries if "issue(id:" in q], [(RECHECK, {"i": "TASK-1"})]
        )
        todo = [q for q, _ in fake.queries].index(fake.reads()[0])
        self.assertEqual([q for q, _ in fake.queries[todo + 1 :]], [RECHECK, fake.mutations[0][0]])

    def test_fr22_every_resumable_issue_is_checked(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "In Progress", "researcher", priority=1),
                issue("TASK-2", "In Progress", "researcher", priority=2),
            ],
            self.hist,
        )
        self.resumable("TASK-1", "a", 60)
        self.resumable("TASK-2", "b", 60, "pm")
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )
        self.assertEqual(
            (fake.issues["TASK-1"]["state"], fake.issues["TASK-2"]["state"]),
            ("In Progress", "In Review"),
        )
        self.assertIn(
            "recover TASK-2 to=in_review reason=role (pm) is not the assignee's (researcher)",
            self.said(),
        )


class Gate(Base):
    """The usage gate's thresholds over the Driver's Usage (the old rate_limit_event rows)."""

    def gate(self):
        self.write_runs()
        return router.Gate(self.runs, NOW, dry=False, driver=self.api)

    def test_thresholds(self):
        self.assertEqual((router.MAX_5H, router.BRAKE_5H, router.MAX_WEEK), (90, 80, 100))
        rows = (
            (reading(89.9, 99.9), None),
            (reading(90, 10), "reason=usage usage=five_hour=90% weekly=10%"),
            (reading(10, 100), "reason=usage usage=five_hour=10% weekly=100%"),
            (reading(150, None), "reason=usage usage=five_hour=150% weekly=none"),
            (reading(None, 99), None),
            (reading(89, None), None),
            (reading(None, None), None),
            (None, "reason=usage unknown"),
            (ConfigError("no runtime a"), "reason=usage unknown"),
            (RuntimeError("boom"), "reason=usage unknown"),
        )
        for value, why in rows:
            with self.subTest(value=value):
                self.api.usages = {"a": value}
                with redirect_stderr(io.StringIO()), self.log_dir(again=False):
                    self.assertEqual(self.gate().blocked("a", "TASK-1"), why is not None)
                self.raw = ""
                self.assertEqual(self.said(), [f"usage-skip TASK-1 runtime=a {why}"] if why else [])

    def test_one_probe_per_runtime(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        with redirect_stderr(io.StringIO()), self.log_dir(again=False):
            gate = self.gate()
            got = [
                gate.blocked(r, i) for r, i in (("a", "TASK-1"), ("b", "TASK-2"), ("a", "TASK-3"))
            ]
        self.assertEqual((got, self.api.usage_calls), ([True, False, True], ["a", "b"]))

    def test_summary(self):
        rows = (
            (None, "unknown"),
            (reading(95, 10.5), "five_hour=95% weekly=10.5%"),
            (reading(None, 100), "five_hour=none weekly=100%"),
            (reading(89.94, None), "five_hour=89.94% weekly=none"),
        )
        for usage, out in rows:
            self.assertEqual(router.summary(usage), out)


class UsageGate(Base):
    """FR-48: each candidate gates on its own runtime's usage."""

    def test_a_blocked_head_runtime_lets_the_next_runtime_claim(self):
        rows = (
            (reading(95, 10), "reason=usage usage=five_hour=95% weekly=10%"),
            (reading(10, 100), "reason=usage usage=five_hour=10% weekly=100%"),
            (None, "reason=usage unknown"),
            (ConfigError("no runtime a"), "reason=usage unknown"),
        )
        for value, why in rows:
            with self.subTest(why=why):
                self.api.usages, self.api.usage_calls = {"a": value, "b": reading(50)}, []
                fake = two()
                self.assertEqual(
                    self.claim(fake, gate=True), f"TASK-2 https://linear.app/x/TASK-2 {PD}"
                )
                self.assertEqual(
                    self.said(),
                    [
                        "pick TASK-1 queue=2",
                        f"usage-skip TASK-1 runtime=a {why}",
                        "pick TASK-2 queue=2",
                        "claim TASK-2 role=pm runtime=b",
                    ],
                )
                self.assertEqual([v["i"] for _, v in fake.mutations], ["TASK-2"])
                self.assertEqual(self.api.usage_calls, ["a", "b"])

    def test_an_absent_window_never_blocks(self):
        self.api.usages = {"a": reading(None, 10)}
        self.assertEqual(
            self.claim(two(), gate=True), "TASK-1 https://linear.app/x/TASK-1 Deep Research"
        )

    def test_two_candidates_of_a_runtime_probe_it_once(self):
        self.api.usages = {"a": reading(95)}
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-3", "Todo", "researcher", priority=2),
                issue("TASK-2", "Todo", "pm", priority=3, project=PD),
            ]
        )
        self.assertEqual(self.claim(fake, gate=True), f"TASK-2 https://linear.app/x/TASK-2 {PD}")
        why = "runtime=a reason=usage usage=five_hour=95% weekly=10%"
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=3",
                f"usage-skip TASK-1 {why}",
                "pick TASK-3 queue=3",
                f"usage-skip TASK-3 {why}",
                "pick TASK-2 queue=3",
                "claim TASK-2 role=pm runtime=b",
            ],
        )
        self.assertEqual(self.api.usage_calls, ["a", "b"])

    def test_all_blocked_claims_nothing(self):
        self.api.usages = {"a": reading(95), "b": None}
        fake = two()
        self.assertEqual(self.claim(fake, gate=True), "")
        self.assertEqual(fake.mutations, [])
        self.assertEqual(self.said()[-1], "pick-none reason=nothing claimable")

    def test_the_claim_gates_on_the_rechecked_roles_runtime(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        rows = (
            ("researcher", "pm", f"TASK-1 https://linear.app/x/TASK-1 {DR}", ["b"]),
            ("pm", "researcher", "", ["a"]),
        )
        for listed, now, out, calls in rows:
            with self.subTest(listed=listed):
                self.api.usage_calls = []
                fake = Changed(
                    [issue("TASK-1", "Todo", listed)], {"TASK-1": {"assignee": who(now)}}
                )
                self.assertEqual(self.claim(fake, gate=True), out)
                self.assertEqual(self.api.usage_calls, calls)
                self.assertEqual(len(fake.mutations), int(bool(out)))

    def test_a_usage_skip_is_said_once_a_day(self):
        self.api.usages = {"a": reading(95)}
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        skip = "usage-skip TASK-1 runtime=a reason=usage usage=five_hour=95% weekly=10%"
        for again, said in ((False, [skip]), (True, [])):
            self.assertEqual(self.claim(fake, gate=True, again=again), "")
            self.assertEqual(
                self.said(), ["pick TASK-1 queue=1", *said, "pick-none reason=nothing claimable"]
            )
        self.assertEqual(self.api.usage_calls, ["a", "a"])

    def test_dry_run_prints_the_usage_of_each_runtime(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        self.limit("a", at)
        fake = two()
        self.assertEqual(
            self.claim(fake, dry=True, gate=True), f"TASK-2 https://linear.app/x/TASK-2 {PD}"
        )
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=2",
                f"usage runtime=a until={at} allowed=false",
                f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                "pick TASK-2 queue=2",
                "usage runtime=b usage=five_hour=10% weekly=10% allowed=true",
            ],
        )
        self.assertEqual(self.taken[1:], (None, "pm", "b"))
        self.assertEqual((fake.mutations, self.api.usage_calls), ([], ["b"]))
        self.assertNotIn(RECHECK, [q for q, _ in fake.queries])
