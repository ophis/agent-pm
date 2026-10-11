import json
import os
import shlex
import sys
import time
from unittest import mock

from adapters.base import Usage
from adapters.codex import CodexAdapter
from driver import api, registry
from new.tests.integration.adapters.codex import fake_app_server
from new.tests.unit.adapters.codex.support import Base


class UsageIntegrationTest(Base):
    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        executable = self.bin / "codex"
        executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                              + shlex.quote(fake_app_server.__file__) + ' "$@"\n')
        executable.chmod(0o755)
        self.log = self.root / "probe.jsonl"
        self.enterContext(mock.patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                                                      "CODEX_THREAD_ID": "inherited"}))

    def probe(self, case="normal", timeout=2):
        adapter = CodexAdapter({"shell_network": False, "env": {"FAKE_LOG": str(self.log), "FAKE_CASE": case}})
        started = time.monotonic()
        result = adapter.common.usage(timeout=timeout)
        self.assertLess(time.monotonic() - started, timeout + 2)
        if self.log.exists():
            rows = [json.loads(line) for line in self.log.read_text().splitlines()]
            with self.assertRaises(ProcessLookupError):
                os.kill(rows[0]["pid"], 0)
        return result

    def test_one_light_probe_matches_ids_and_maps_window_duration(self):
        self.assertEqual(self.probe(), Usage(62, False, 2000, 4000))
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(rows[0]["argv"], ["app-server"])
        self.assertIsNone(rows[0]["marker"])
        self.assertEqual([row["method"] for row in rows[1:]],
                         ["initialize", "initialized", "account/rateLimits/read"])
        self.assertEqual(rows[1]["id"], 1)
        self.assertEqual(rows[3]["id"], 2)

    def test_fallback_null_and_weekly_windows(self):
        for case, expected in (("fallback", Usage(62, False, 2000, 4000)),
                               ("weekly-full", Usage(62, True, 2000, 4000)),
                               ("weekly-only", Usage(None, False, None, 4000)),
                               ("null", Usage()), ("invalid-window", Usage()), ("huge-number", Usage())):
            with self.subTest(case=case):
                self.log.unlink(missing_ok=True)
                self.assertEqual(self.probe(case), expected)

    def test_errors_malformed_absent_and_timeout_are_unknown_and_cleaned_up(self):
        for case in ("error", "init-error", "malformed", "oversize", "exit", "silent", "wrong-id", "ignore-term"):
            with self.subTest(case=case):
                self.log.unlink(missing_ok=True)
                expected = Usage(62, False, 2000, 4000) if case == "ignore-term" else Usage()
                self.assertEqual(self.probe(case, timeout=0.3), expected)
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError("missing executable")):
            self.assertEqual(self.probe(), Usage())

    def test_driver_account_query_uses_factory_and_does_not_start_model(self):
        config = {"adapters": {"codex": {"implementation": "adapters.codex:CodexAdapter", "shell_network": False,
                                        "env": {"FAKE_LOG": str(self.log)}}}}
        with mock.patch.object(registry, "read_config", return_value=config):
            self.assertEqual(api.usage("codex"), Usage(62, False, 2000, 4000))

    def test_native_environment_filters_markers_and_keeps_configured_overrides(self):
        common = CodexAdapter({"shell_network": False, "env": {
            "FAKE_LOG": str(self.log), "FAKE_ADDITION": "configured", "PAGER": "configured"}}).common
        inherited = {key: "inherited" for key in (*common.unset, *common.defaults)}
        with mock.patch.dict(os.environ, inherited):
            self.assertEqual(common.usage(timeout=2), Usage(62, False, 2000, 4000))
        env = json.loads(self.log.read_text().splitlines()[0])["env"]
        for key in common.unset:
            self.assertIsNone(env[key], key)
        for key, value in common.defaults.items():
            self.assertEqual(env[key], "configured" if key == "PAGER" else value, key)
        self.assertEqual((env["CODEX_HOME"], env["FAKE_ADDITION"]), (str(self.codex_home), "configured"))
