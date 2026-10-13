import os

from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.api_errors_fixtures import ApiErrors as ApiErrorsBase


class ApiErrors1(ApiErrorsBase):
    def test_once_a_day_per_reason_printed_only_on_a_terminal(self):
        timeout, limit = (
            linear.Unavailable("teams", reason="TimeoutError: timed out"),
            linear.Unavailable("commentCreate", status=400, reason="RATELIMITED"),
        )
        line = {"src": "router", "kind": "linear-error"}
        cases = [
            (linear.Unavailable("teams", status=502), False, "", {"op": "teams", "status": 502}),
            (
                linear.Unavailable("issues", status=503),
                True,
                "router.py: Linear unavailable: issues: HTTP 503\n",
                None,
            ),
            (
                timeout,
                True,
                self.json(**line, op="teams", reason="TimeoutError: timed out")
                + "router.py: Linear unavailable: teams: TimeoutError: timed out\n",
                {"op": "teams", "reason": "TimeoutError: timed out"},
            ),
            (limit, False, "", {"op": "commentCreate", "status": 400, "reason": "RATELIMITED"}),
            (limit, False, "", None),
            (timeout, False, "", None),
        ]
        written = []
        for e, tty, err, new in cases:
            with self.subTest(e=str(e), tty=tty):
                self.assertEqual(self.error(e, tty), (1, err))
                written += [{"ts": self.TS, **line, **new}] if new else []
                self.assertEqual(self.lines(), written)
        self.assertEqual(self.error(timeout, src="promote"), (1, ""))
        self.assertEqual(
            self.lines()[-1],
            {
                "ts": self.TS,
                "src": "promote",
                "kind": "linear-error",
                "op": "teams",
                "reason": "TimeoutError: timed out",
            },
        )

    def test_dry_prints_the_line_and_writes_nothing(self):
        e = linear.Unavailable("teams", status=503)
        for tty in (False, True):
            for _ in range(2):
                self.assertEqual(
                    self.error(e, tty, dry=True),
                    (1, self.json("router", "linear-error", op="teams", status=503)),
                )
        self.assertFalse(os.path.exists(self.logs))
