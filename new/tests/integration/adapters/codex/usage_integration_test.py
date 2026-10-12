from new.tests.integration.adapters.codex import fake_app_server
from new.tests.unit.adapters.codex.support import Base

import json
import os
import shlex
import sys
import time
from unittest import mock

from agent_pm.adapters.base import Usage, Window
from agent_pm.driver import api, registry

FIVE, WEEK = Window(62, 2000), Window(41, 4000)


class UsageIntegrationTest(Base):
    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        executable = self.bin / "codex"
        executable.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(fake_app_server.__file__)
            + ' "$@"\n'
        )
        executable.chmod(0o755)
        self.log = self.root / "probe.jsonl"
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "CODEX_THREAD_ID": "inherited",
                },
            )
        )

    def probe(self, case="normal", **env):
        registry.configure(
            {
                "adapters": {
                    "codex": {
                        "implementation": "adapters.codex:CodexAdapter",
                        "shell_network": False,
                        "env": {"FAKE_LOG": str(self.log), "FAKE_CASE": case, **env},
                    }
                }
            }
        )
        started = time.monotonic()
        result = api.usage("codex")
        self.assertLess(time.monotonic() - started, 12)
        if self.log.exists():
            rows = [json.loads(line) for line in self.log.read_text().splitlines()]
            with self.assertRaises(ProcessLookupError):
                os.kill(rows[0]["pid"], 0)
        return result

    def test_one_light_probe_matches_ids_and_maps_window_duration(self):
        self.assertEqual(self.probe(), Usage(FIVE, WEEK))
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(rows[0]["argv"], ["app-server"])
        self.assertIsNone(rows[0]["marker"])
        self.assertEqual(
            [row["method"] for row in rows[1:]],
            ["initialize", "initialized", "account/rateLimits/read"],
        )
        self.assertEqual(rows[1]["id"], 1)
        self.assertEqual(rows[3]["id"], 2)

    def test_fallback_null_and_weekly_windows(self):
        for case, expected in (
            ("fallback", Usage(FIVE, WEEK)),
            ("five-first", Usage(FIVE, WEEK)),
            ("weekly-full", Usage(FIVE, Window(100, 4000))),
            ("weekly-only", Usage(None, WEEK)),
            ("null", Usage(None, None)),
            ("invalid-window", Usage(None, None)),
            ("huge-number", Usage(None, None)),
            ("empty", None),
        ):
            with self.subTest(case=case):
                self.log.unlink(missing_ok=True)
                self.assertEqual(self.probe(case), expected)

    def test_errors_malformed_absent_and_timeout_are_unknown_and_cleaned_up(self):
        for case in (
            "error",
            "init-error",
            "malformed",
            "oversize",
            "exit",
            "null-result",
            "stderr",
            "ignore-term",
        ):
            with self.subTest(case=case):
                self.log.unlink(missing_ok=True)
                expected = Usage(FIVE, WEEK) if case == "ignore-term" else None
                self.assertEqual(self.probe(case), expected)
        self.log.unlink(missing_ok=True)
        self.assertIsNone(self.probe(PATH=str(self.work)))
        self.assertFalse(self.log.exists())

    def test_driver_hosted_helper_timeout_is_unknown_and_reaped(self):
        self.assertIsNone(self.probe("silent"))

    def test_driver_account_query_uses_factory_and_does_not_start_model(self):
        self.assertEqual(self.probe(), Usage(FIVE, WEEK))
        row = json.loads(self.log.read_text().splitlines()[0])
        self.assertNotEqual(row["ppid"], os.getpid())
        self.assertEqual(row["cwd"], str(self.home / ".agent-pm" / "probe"))
        self.assertEqual(row["env"]["PWD"], row["cwd"])
        self.assertFalse((self.work / "run.jsonl").exists())

    def test_native_environment_filters_markers_and_keeps_configured_overrides(self):
        common = self.common
        inherited = {key: "inherited" for key in (*common.unset, *common.defaults)}
        with mock.patch.dict(os.environ, inherited):
            self.assertEqual(
                self.probe(FAKE_ADDITION="configured", PAGER="configured"), Usage(FIVE, WEEK)
            )
        env = json.loads(self.log.read_text().splitlines()[0])["env"]
        for key in common.unset:
            self.assertIsNone(env[key], key)
        for key, value in common.defaults.items():
            self.assertEqual(env[key], "configured" if key == "PAGER" else value, key)
        self.assertEqual(
            (env["CODEX_HOME"], env["FAKE_ADDITION"]), (str(self.codex_home), "configured")
        )
