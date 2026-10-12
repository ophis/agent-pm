from new.tests.unit.adapters.codex.support import CHILD, NATIVE, Base

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from adapters.base import Launch, SessionStatus, Usage, Window
from adapters.codex import CodexAdapter

FIVE, WEEK = Window(62, 2000), Window(41, 4000)


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
                self.assertEqual(self.adapter.usage(self.output(primary, secondary)), Usage(FIVE, WEEK))
            spawn.assert_not_called()

    def test_codex_limit_is_preferred_and_legacy_rate_limits_are_fallback(self):
        data = {"rateLimitsByLimitId": {"codex": {"primary": self.weekly, "secondary": self.five}},
                "rateLimits": {"primary": None, "secondary": None}}
        self.assertEqual(self.adapter.usage(json.dumps(data)), Usage(FIVE, WEEK))
        data["rateLimitsByLimitId"] = {"other": {"primary": None, "secondary": None}}
        data["rateLimits"] = {"primary": self.five, "secondary": self.weekly}
        self.assertEqual(self.adapter.usage(json.dumps(data)), Usage(FIVE, WEEK))
        data["rateLimitsByLimitId"]["codex"] = "bad"
        self.assertEqual(self.adapter.usage(json.dumps(data)), Usage(FIVE, WEEK))
        data["rateLimitsByLimitId"]["codex"] = {"primary": self.weekly, "secondary": None}
        self.assertEqual(self.adapter.usage(json.dumps(data)), Usage(None, WEEK))

    def test_each_window_alone_and_neither_is_a_reading(self):
        for snapshot, expected in (({"primary": self.five, "secondary": None}, Usage(FIVE, None)),
                                   ({"primary": None, "secondary": self.weekly}, Usage(None, WEEK)),
                                   ({"secondary": self.five}, Usage(FIVE, None)),
                                   ({"primary": "bad", "secondary": [self.weekly]}, Usage(None, None)),
                                   ({"primary": {**self.five, "windowDurationMins": 60}}, Usage(None, None)),
                                   ({"primary": None, "secondary": None}, Usage(None, None)), ({}, Usage(None, None))):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(self.adapter.usage(json.dumps({"rateLimits": snapshot})), expected)

    def test_percent_may_exceed_one_hundred_and_each_window_keeps_its_reset(self):
        self.assertEqual(self.adapter.usage(self.output({**self.five, "usedPercent": 125},
                                                        {**self.weekly, "usedPercent": 100})),
                         Usage(Window(125, 2000), Window(100, 4000)))
        self.assertEqual(self.adapter.usage(self.output({**self.five, "usedPercent": 0, "resetsAt": 0},
                                                        {**self.weekly, "usedPercent": 99.5, "resetsAt": 4000.5})),
                         Usage(Window(0, 0), Window(99.5, 4000.5)))

    def test_missing_or_unreliable_reset_is_unknown_in_its_window_only(self):
        for value in (None, "bad", True, -1, float("inf"), float("nan"), 10 ** 1000):
            with self.subTest(value=value):
                self.assertEqual(self.adapter.usage(self.output(primary={**self.five, "resetsAt": value})),
                                 Usage(Window(62, None), WEEK))
                self.assertEqual(self.adapter.usage(self.output(secondary={**self.weekly, "resetsAt": value})),
                                 Usage(FIVE, Window(41, None)))
        absent = {key: value for key, value in self.weekly.items() if key != "resetsAt"}
        self.assertEqual(self.adapter.usage(self.output(secondary=absent)), Usage(FIVE, Window(41, None)))

    def test_invalid_percent_or_duration_drops_only_its_window(self):
        for value in (None, True, "62", -1, float("inf"), float("nan"), 10 ** 1000):
            with self.subTest(value=value):
                self.assertEqual(self.adapter.usage(self.output(primary={**self.five, "usedPercent": value})),
                                 Usage(None, WEEK))
                self.assertEqual(self.adapter.usage(self.output(secondary={**self.weekly, "usedPercent": value})),
                                 Usage(FIVE, None))
        absent = {key: value for key, value in self.five.items() if key != "usedPercent"}
        self.assertEqual(self.adapter.usage(self.output(primary=absent)), Usage(None, WEEK))
        for duration in (300.0, "300", True, None):
            with self.subTest(duration=duration):
                self.assertEqual(self.adapter.usage(self.output(primary={**self.five, "windowDurationMins": duration})),
                                 Usage(None, WEEK))

    def test_the_first_window_of_a_duration_wins(self):
        later = {**self.five, "usedPercent": 10, "resetsAt": 3000}
        self.assertEqual(self.adapter.usage(self.output(self.five, later)), Usage(FIVE, None))
        self.assertEqual(self.adapter.usage(self.output({**self.five, "usedPercent": None}, later)), Usage(None, None))

    def test_output_without_a_snapshot_is_a_failed_probe(self):
        for output in ("", "null", "{}", "[]", '"text"', "{bad json", "[" * 2000,
                       json.dumps({"rateLimits": None}), json.dumps({"rateLimits": [self.five]}),
                       json.dumps({"rateLimitsByLimitId": {"codex": None}}),
                       json.dumps({"rateLimitsByLimitId": "codex", "rateLimits": "bad"})):
            with self.subTest(output=output[:80]):
                self.assertIsNone(self.adapter.usage(output))
