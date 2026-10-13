import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from unittest import mock

from agent_pm.linear_orchestrator.engine import config, router
from agent_pm.session import ConfigError, SessionStatus
from orchestrator_tests.unit.clock import frozen
from orchestrator_tests.unit.router.data import (
    CONFIG,
    DR,
    EAST,
    NOW,
    OVERLAY,
    PD,
    RECHECK,
    ago,
    issue,
    reading,
    who,
)
from orchestrator_tests.unit.router.fakes import Changed, FakeLinear, two
from orchestrator_tests.unit.router.fixtures import (
    Base,
    TickBase,
)


class RuntimeUsage(Base):
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


class ClaimUsageGates(Base):
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


class RunBrake(Base):
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


class ScheduledUsageGates(TickBase):
    """FR-46, FR-48, FR-60 over a tick: each candidate gates on its runtime; a resume keeps its
    recorded runtime.
    """

    def test_a_resume_keeps_the_runtime_of_its_claim(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with frozen(ago(minutes=60)):
            self.tick(fake)
        (claimed,) = self.start.launches
        self.assertEqual(self.last_run(), ("start", "TASK-1", claimed.sid, "researcher", "a"))
        with open(self.runs) as f:
            self.lines = f.read().splitlines()
        self.api.statuses[claimed.sid] = SessionStatus(
            "yes", (NOW - timedelta(minutes=40)).timestamp(), ""
        )
        self.write_config(CONFIG.replace(OVERLAY, '[core]\nruntime = "b"\n'))
        self.tick(fake)
        (resumed,) = self.start.launches
        self.assertEqual((resumed.sid, resumed.mode, resumed.runtime), (claimed.sid, "resume", "a"))
        self.assertEqual(self.last_run(), ("resume", "TASK-1", claimed.sid, "researcher", "a"))
        self.assertEqual(
            (self.api.info, self.api.usage_calls),
            ([("a", claimed.sid, os.path.join(self.work, "work", "TASK-1"))], ["a"]),
        )

    def test_a_blocked_resume_candidate_lets_a_claim_start(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=1),
            issue("TASK-2", "Todo", "pm", priority=2, project=PD),
        )
        self.resumable("TASK-1", "a", 60)
        self.assertEqual(self.tick(fake), 0)
        self.assertEqual(
            self.said(),
            [
                "usage-skip TASK-1 runtime=a reason=usage usage=five_hour=95% weekly=10%",
                "plan mode=new queue=1",
                "pick TASK-2 queue=1",
                "claim TASK-2 role=pm runtime=b",
                "launch TASK-2 exit=0",
            ],
        )
        (launch,) = self.start.launches
        self.assertEqual((launch.issue, launch.runtime), ("TASK-2", "b"))
        self.assertEqual(self.last_run(), ("start", "TASK-2", launch.sid, "pm", "b"))
        self.assertEqual(self.api.usage_calls, ["a", "b"])
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_all_blocked_starts_nothing_and_probes_a_runtime_once(self):
        self.api.usages = {"a": reading(95)}
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=1),
            issue("TASK-3", "In Progress", "engineer", priority=2, project=PD),
            issue("TASK-2", "Todo", "researcher", priority=2),
        )
        self.resumable("TASK-1", "a", 60)
        self.resumable("TASK-3", "c", 50, "engineer")
        before = self.before()
        why = "runtime=a reason=usage usage=five_hour=95% weekly=10%"
        for again, said in ((False, [f"usage-skip TASK-{n} {why}" for n in (1, 3, 2)]), (True, [])):
            with self.subTest(again=again):
                self.assertEqual(self.tick(fake, again=again), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual(
                    (self.start.launches, fake.mutations, self.state, self.api.usage_calls),
                    ([], [], before, ["a"]),
                )

    def test_a_live_limit_row_lets_the_other_runtime_start(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        self.limit("a", at)
        self.tick(two())
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=2",
                "pick TASK-1 queue=2",
                f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                "pick TASK-2 queue=2",
                "claim TASK-2 role=pm runtime=b",
                "launch TASK-2 exit=0",
            ],
        )
        self.assertEqual((self.launched_runs(), self.api.usage_calls), ([("TASK-2", "new")], ["b"]))
        self.assertEqual(
            [json.loads(x)["kind"] for x in self.state.splitlines()], ["limit", "start"]
        )

    def test_a_dry_run_walks_the_same_and_changes_nothing(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        b = "usage runtime=b usage=five_hour=10% weekly=10% allowed=true"
        rows = (
            (
                True,
                [
                    f"usage runtime=a until={at} allowed=false",
                    f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                    "plan mode=new queue=1",
                    "pick TASK-2 queue=1",
                    b,
                ],
                ["b"],
            ),
            (
                False,
                [
                    "usage runtime=a usage=five_hour=10% weekly=10% allowed=true",
                    "plan TASK-1 mode=resume sid=a",
                ],
                ["a"],
            ),
        )
        for limited, said, calls in rows:
            with self.subTest(limited=limited):
                self.lines, self.hist = [], {}
                if limited:
                    self.limit("a", at)
                fake = self.fake(
                    issue("TASK-1", "In Progress", "researcher", priority=1),
                    issue("TASK-2", "Todo", "pm", priority=2, project=PD),
                )
                self.resumable("TASK-1", "a", 60)
                before = self.before()
                self.assertEqual(self.tick(fake, "--dry-run"), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual(
                    (self.start.launches, fake.mutations, self.state, self.api.usage_calls),
                    ([], [], before, calls),
                )
