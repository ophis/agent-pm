import subprocess
import threading
import time

from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import sessions
from orchestrator_test_helpers.unit.sessions.data import CID, ISSUE, SID, T1, comment, end, start
from orchestrator_test_helpers.unit.sessions.failure_fixtures import Failure as FailureBase
from orchestrator_test_helpers.unit.sessions.fake_linear_fixtures import FakeLinear


class Failure1(FailureBase):
    def test_update_failure_never_creates(self):
        for exc, reason in (
            (RuntimeError("boom"), "RuntimeError: boom"),
            (
                LinearError("linear api error: [{'message': 'nope'}]"),
                "LinearError: linear api error: [{'message': 'nope'}]",
            ),
        ):
            with self.subTest(reason=reason):
                fake = FakeLinear(fail={"commentUpdate": exc})
                self.assertEqual(start(fake, self.record()), reason)
                self.assertEqual(fake.calls, ["commentUpdate"])
        calls = []

        def malformed(query, **_v):
            calls.append(query)
            return {}

        self.assertRegex(end(malformed, self.record(), 0), r"\AKeyError: [^\n]+\Z")
        self.assertEqual(len(calls), 1)

    def test_write_failure_logs_one_line(self):
        existing = comment(CID, f"Run {SID} · running · #1 · {T1}")
        for op, seed, calls in (
            ("commentCreate", (), ["commentUpdate", "commentCreate"]),
            ("commentUpdate", (existing,), ["commentUpdate"]),
        ):
            for exc, reason in (
                (RuntimeError("boom"), "RuntimeError: boom"),
                (
                    LinearError("linear api error: [{'message': 'nope'}]"),
                    "LinearError: linear api error: [{'message': 'nope'}]",
                ),
                (RuntimeError("two\nlines"), "RuntimeError: two lines"),
                (False, "success: false"),
            ):
                with self.subTest(op=op, reason=reason):
                    fake = FakeLinear(*seed, fail={op: exc})
                    self.assertEqual(start(fake, self.record()), reason)
                    self.assertEqual(fake.calls, calls)

    def test_blocking_gql_times_out(self):
        release = threading.Event()
        self.addCleanup(release.set)
        t = time.monotonic()
        out = sessions.post(
            ISSUE, self.record(), 0, gql=lambda _q, **_v: release.wait(30), timeout=0.1
        )
        self.assertLess(time.monotonic() - t, 5)
        self.assertEqual(out, "timed out after 0.1s")

    def test_secret_outside_the_message_does_not_leak(self):
        exc = subprocess.CalledProcessError(
            1,
            ["security", "find-generic-password", "-s", "svc", "-w"],
            output="lin_api_SECRET",
            stderr="lin_api_SECRET",
        )
        exc.headers = {"Authorization": "lin_api_SECRET"}
        out = start(self.failing(exc), self.record())
        self.assertTrue(out.startswith("CalledProcessError: "), out)
        self.assertNotIn("lin_api_SECRET", out)
