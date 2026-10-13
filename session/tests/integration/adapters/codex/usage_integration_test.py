"""Codex Driver usage queries and environment isolation through a fake app-server."""

import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from agent_pm.session.adapters.codex import CodexAdapter
from agent_pm.session.engine.types import Usage, Window

from session.tests.case import REPO, Case
from session.tests.integration.adapters.codex import fake_app_server

FIVE, WEEK = Window(62, 2000), Window(41, 4000)


class UsageIntegrationTest(Case):
    def test_fixture_preserves_filesystem_checks_outside_its_scratch(self):
        self.assertTrue(os.path.lexists(__file__))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", REPO))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.work = (self.root / name for name in ("home", "work"))
        self.codex_home = self.home / ".codex"
        for path in (self.work, self.codex_home):
            path.mkdir(parents=True, exist_ok=True)
        self.common = CodexAdapter({}).common
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
                    "HOME": str(self.home),
                    "CODEX_HOME": str(self.codex_home),
                    "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "CODEX_THREAD_ID": "inherited",
                },
            )
        )

    def probe(self, case="normal", **env):
        self.configure_session(
            {
                "codex": {
                    "implementation": "adapters.codex:CodexAdapter",
                    "env": {"FAKE_LOG": str(self.log), "FAKE_CASE": case, **env},
                }
            }
        )
        started = time.monotonic()
        result = self.driver.usage("codex")
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
        inherited = {key: "inherited" for key in (*common.UNSET, *common.DEFAULTS)}
        with mock.patch.dict(os.environ, inherited):
            self.assertEqual(
                self.probe(FAKE_ADDITION="configured", PAGER="configured"), Usage(FIVE, WEEK)
            )
        env = json.loads(self.log.read_text().splitlines()[0])["env"]
        for key in common.UNSET:
            self.assertIsNone(env[key], key)
        for key, value in common.DEFAULTS.items():
            self.assertEqual(env[key], "configured" if key == "PAGER" else value, key)
        self.assertEqual(
            (env["CODEX_HOME"], env["FAKE_ADDITION"]), (str(self.codex_home), "configured")
        )
