import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from adapters.base import Launch, SessionStatus, Usage
from adapters.codex import CodexAdapter

from new.tests.unit.adapters.codex.support import CHILD, NATIVE, Base


class InspectionTest(Base):
    def test_adapter_inspects_binding_session_and_descendant_by_cwd(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        self.rollout(CHILD, parent=NATIVE, stamp=300)
        info = self.adapter.session_status(self.spec.sid, str(self.cwd))
        self.assertIsInstance(info, SessionStatus)
        self.assertEqual((info.resumable, info.last_active), ("yes", 300))
        self.assertIn("codex resume", info.resume_command)
        self.assertIn(NATIVE, info.resume_command)
        self.assertIn("CODEX_HOME=", info.resume_command)

    def test_missing_and_unreadable_storage_have_distinct_status(self):
        self.assertEqual(self.adapter.session_status(self.spec.sid, str(self.cwd)), SessionStatus("no", None, ""))
        self.prepare()
        self.assertEqual(self.adapter.session_status(self.spec.sid, str(self.cwd)), SessionStatus("no", None, ""))
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.assertEqual(self.adapter.session_status(self.spec.sid, str(self.cwd)).resumable, "no")
        path = self.rollout()
        with mock.patch("os.open", side_effect=PermissionError("unreadable")):
            self.assertEqual(self.adapter.session_status(self.spec.sid, str(self.cwd)).resumable, "unknown")
        path.unlink()
        self.assertEqual(self.adapter.session_status(self.spec.sid, str(self.cwd)).resumable, "no")


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch("subprocess.Popen", side_effect=AssertionError("usage parser started a process")))
        self.adapter = CodexAdapter({"shell_network": False, "env": {"PAGER": "configured", "ADDED": "value"}})
        self.five = {"usedPercent": 62, "windowDurationMins": 300, "resetsAt": 2000}
        self.weekly = {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": 4000}

    def output(self, primary=None, secondary=None):
        return json.dumps({"rateLimits": {"primary": self.five if primary is None else primary,
                                           "secondary": self.weekly if secondary is None else secondary}})

    def test_probe_launch_uses_absolute_python_and_script_and_native_environment(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "inherited", "PAGER": "inherited"}):
            launch = self.adapter.usage_probe()
        self.assertIsInstance(launch, Launch)
        self.assertEqual(launch.argv[0], os.path.realpath(sys.executable))
        self.assertTrue(all(os.path.isabs(path) for path in launch.argv))
        self.assertEqual(Path(launch.argv[1]).name, "account_probe.py")
        self.assertTrue(Path(launch.argv[1]).is_file())
        self.assertEqual(launch.env, {**self.adapter.common.defaults, "PAGER": "configured", "ADDED": "value"})
        self.assertEqual(launch.unset, self.adapter.common.unset)
        self.assertEqual(launch.cwd, "")

    def test_usage_is_pure_and_matches_both_window_positions(self):
        with mock.patch("subprocess.Popen") as spawn:
            for primary, secondary in ((self.five, self.weekly), (self.weekly, self.five)):
                self.assertEqual(self.adapter.usage(self.output(primary, secondary)), Usage(62, False, 2000))
            spawn.assert_not_called()

    def test_codex_limit_is_preferred_and_legacy_rate_limits_are_fallback(self):
        data = {"rateLimitsByLimitId": {"codex": {"primary": self.weekly, "secondary": self.five}},
                "rateLimits": {"primary": None, "secondary": None}}
        self.assertEqual(self.adapter.usage(json.dumps(data)), Usage(62, False, 2000))
        data["rateLimitsByLimitId"] = {"other": {"primary": None, "secondary": None}}
        data["rateLimits"] = {"primary": self.five, "secondary": self.weekly}
        self.assertEqual(self.adapter.usage(json.dumps(data)), Usage(62, False, 2000))
        data["rateLimitsByLimitId"]["codex"] = {"primary": self.weekly, "secondary": None}
        self.assertIsNone(self.adapter.usage(json.dumps(data)))

    def test_weekly_full_selects_its_reset_and_percent_may_exceed_one_hundred(self):
        self.assertEqual(self.adapter.usage(self.output({**self.five, "usedPercent": 125},
                                                        {**self.weekly, "usedPercent": 100})),
                         Usage(125, True, 4000))
        self.assertEqual(self.adapter.usage(self.output(secondary={**self.weekly, "usedPercent": 99})),
                         Usage(62, False, 2000))

    def test_missing_or_unreliable_reset_is_unknown_with_a_complete_reading(self):
        for value in (None, "bad", True, -1, float("inf")):
            with self.subTest(value=value):
                self.assertEqual(self.adapter.usage(self.output(primary={**self.five, "resetsAt": value})),
                                 Usage(62, False, None))
        self.assertEqual(self.adapter.usage(self.output(secondary={**self.weekly, "usedPercent": 100,
                                                                  "resetsAt": None})), Usage(62, True, None))

    def test_partial_null_and_malformed_readings_are_whole_value_unknown(self):
        for output in ("null", "{}", "[]", "{bad json", "[" * 2000,
                       json.dumps({"rateLimits": {"primary": self.weekly, "secondary": None}}),
                       json.dumps({"rateLimits": {"primary": None, "secondary": self.five}}),
                       self.output(self.five, self.five)):
            with self.subTest(output=output[:80]):
                self.assertIsNone(self.adapter.usage(output))

    def test_invalid_percent_and_duration_are_unknown_without_invented_fields(self):
        for value in (None, True, "62", -1, float("inf"), float("nan"), 10 ** 1000):
            for duration in (300, 10080):
                with self.subTest(value=value, duration=duration):
                    window = {"usedPercent": value, "windowDurationMins": duration, "resetsAt": 2000}
                    self.assertIsNone(self.adapter.usage(self.output(window if duration == 300 else self.five,
                                                                   window if duration == 10080 else self.weekly)))
        self.assertIsNone(self.adapter.usage(self.output(primary={**self.five, "windowDurationMins": 300.0})))
