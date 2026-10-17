"""Codex account usage and session inspection queries."""

import contextlib
import io
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_pm.session.clients.base import Client
from agent_pm.session.clients.codex import CodexClient
from agent_pm.session.engine.types import (
    Access,
    ConfigError,
    Launch,
    SessionInfo,
    SessionStatus,
    Usage,
    Window,
)
from agent_pm.utils import installations_root

from session.tests.case import REPO, Case
from session.tests.unit.clients.codex.support import CHILD, NATIVE, Base

FIVE, WEEK = Window(62, 2000), Window(41, 4000)


class SessionStatusTest(Base):
    def test_client_inspects_binding_session_and_descendant_by_cwd(self):
        self.prepare()
        state = self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        self.rollout(CHILD, parent=NATIVE, stamp=300)
        info = self.client.session_status(
            SessionInfo(
                sid=self.spec.sid, cwd=str(self.cwd), project=False, transcript="", resume=""
            )
        )
        self.assertIsInstance(info, SessionStatus)
        self.assertEqual((info.resumable, info.last_active), ("yes", 300))
        self.assertEqual(self.common.resume_launch(state).argv[:2], ("codex", "resume"))
        self.assertEqual(info.resume_command, self.common.resume_command(state))
        self.assertIn(NATIVE, info.resume_command)
        self.assertNotIn("CODEX_HOME=", info.resume_command)

    def test_status_validates_a_recorded_transcript_and_rebuilds_resume(self):
        self.prepare()
        state = self.common.bind(str(self.work), self.spec.sid, NATIVE)
        original = self.rollout()
        recorded = Path(self.work, original.name)
        original.rename(recorded)
        info = SessionInfo(
            sid=self.spec.sid,
            cwd=str(self.cwd),
            project=False,
            transcript=str(recorded),
            resume="rm -rf ~",
        )
        self.assertEqual(
            self.client.session_status(info),
            SessionStatus("yes", recorded.stat().st_mtime, self.common.resume_command(state)),
        )

    def test_missing_and_unreadable_storage_have_distinct_status(self):
        self.assertEqual(
            self.client.session_status(
                SessionInfo(
                    sid=self.spec.sid, cwd=str(self.cwd), project=False, transcript="", resume=""
                )
            ),
            SessionStatus("no", None, ""),
        )
        self.prepare()
        self.assertEqual(
            self.client.session_status(
                SessionInfo(
                    sid=self.spec.sid, cwd=str(self.cwd), project=False, transcript="", resume=""
                )
            ),
            SessionStatus("no", None, ""),
        )
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.assertEqual(
            self.client.session_status(
                SessionInfo(
                    sid=self.spec.sid, cwd=str(self.cwd), project=False, transcript="", resume=""
                )
            ).resumable,
            "no",
        )
        path = self.rollout()
        with (
            mock.patch("os.open", side_effect=PermissionError("unreadable")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                self.client.session_status(
                    SessionInfo(
                        sid=self.spec.sid,
                        cwd=str(self.cwd),
                        project=False,
                        transcript="",
                        resume="",
                    )
                ).resumable,
                "unknown",
            )
        path.unlink()
        self.assertEqual(
            self.client.session_status(
                SessionInfo(
                    sid=self.spec.sid, cwd=str(self.cwd), project=False, transcript="", resume=""
                )
            ).resumable,
            "no",
        )


class UsageTest(Case):
    def setUp(self):
        self.enterContext(
            mock.patch(
                "subprocess.Popen", side_effect=AssertionError("usage parser started a process")
            )
        )
        self.client = CodexClient({"env": {"PAGER": "configured", "ADDED": "value"}})
        self.five = {"usedPercent": 62, "windowDurationMins": 300, "resetsAt": 2000}
        self.weekly = {"usedPercent": 41, "windowDurationMins": 10080, "resetsAt": 4000}

    def output(self, primary=None, secondary=None):
        return json.dumps(
            {
                "rateLimits": {
                    "primary": self.five if primary is None else primary,
                    "secondary": self.weekly if secondary is None else secondary,
                }
            }
        )

    def test_probe_launch_uses_absolute_python_and_script_and_native_environment(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "inherited", "PAGER": "inherited"}):
            launch = self.client.usage_probe()
        self.assertIsInstance(launch, Launch)
        self.assertEqual(len(launch.argv), 3)
        self.assertEqual(launch.argv[0], os.path.realpath(sys.executable))
        self.assertEqual(launch.argv[1], "-I")
        self.assertTrue(all(os.path.isabs(path) for path in launch.argv if path != "-I"))
        self.assertEqual(Path(launch.argv[2]).name, "app_server.py")
        self.assertTrue(Path(launch.argv[2]).is_file())
        self.assertEqual(
            launch.env, {**self.client.common.DEFAULTS, "PAGER": "configured", "ADDED": "value"}
        )
        self.assertEqual(launch.unset, self.client.common.UNSET)
        self.assertEqual(launch.cwd, "")

    def test_usage_is_pure_and_matches_both_window_positions(self):
        with mock.patch("subprocess.Popen") as spawn:
            for primary, secondary in ((self.five, self.weekly), (self.weekly, self.five)):
                self.assertEqual(
                    self.client.usage(self.output(primary, secondary)), Usage(FIVE, WEEK)
                )
            spawn.assert_not_called()

    def test_codex_limit_is_preferred_and_legacy_rate_limits_are_fallback(self):
        data = {
            "rateLimitsByLimitId": {"codex": {"primary": self.weekly, "secondary": self.five}},
            "rateLimits": {"primary": None, "secondary": None},
        }
        self.assertEqual(self.client.usage(json.dumps(data)), Usage(FIVE, WEEK))
        data["rateLimitsByLimitId"] = {"other": {"primary": None, "secondary": None}}
        data["rateLimits"] = {"primary": self.five, "secondary": self.weekly}
        self.assertEqual(self.client.usage(json.dumps(data)), Usage(FIVE, WEEK))
        data["rateLimitsByLimitId"]["codex"] = "bad"
        self.assertEqual(self.client.usage(json.dumps(data)), Usage(FIVE, WEEK))
        data["rateLimitsByLimitId"]["codex"] = {"primary": self.weekly, "secondary": None}
        self.assertEqual(self.client.usage(json.dumps(data)), Usage(None, WEEK))

    def test_each_window_alone_and_neither_is_a_reading(self):
        for snapshot, expected in (
            ({"primary": self.five, "secondary": None}, Usage(FIVE, None)),
            ({"primary": None, "secondary": self.weekly}, Usage(None, WEEK)),
            ({"secondary": self.five}, Usage(FIVE, None)),
            ({"primary": "bad", "secondary": [self.weekly]}, Usage(None, None)),
            ({"primary": {**self.five, "windowDurationMins": 60}}, Usage(None, None)),
            ({"primary": None, "secondary": None}, Usage(None, None)),
            ({}, Usage(None, None)),
        ):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(self.client.usage(json.dumps({"rateLimits": snapshot})), expected)

    def test_percent_may_exceed_one_hundred_and_each_window_keeps_its_reset(self):
        self.assertEqual(
            self.client.usage(
                self.output({**self.five, "usedPercent": 125}, {**self.weekly, "usedPercent": 100})
            ),
            Usage(Window(125, 2000), Window(100, 4000)),
        )
        self.assertEqual(
            self.client.usage(
                self.output(
                    {**self.five, "usedPercent": 0, "resetsAt": 0},
                    {**self.weekly, "usedPercent": 99.5, "resetsAt": 4000.5},
                )
            ),
            Usage(Window(0, 0), Window(99.5, 4000.5)),
        )

    def test_missing_or_unreliable_reset_is_unknown_in_its_window_only(self):
        for value in (None, "bad", True, -1, float("inf"), float("nan"), 10**1000):
            with self.subTest(value=value):
                self.assertEqual(
                    self.client.usage(self.output(primary={**self.five, "resetsAt": value})),
                    Usage(Window(62, None), WEEK),
                )
                self.assertEqual(
                    self.client.usage(self.output(secondary={**self.weekly, "resetsAt": value})),
                    Usage(FIVE, Window(41, None)),
                )
        absent = {key: value for key, value in self.weekly.items() if key != "resetsAt"}
        self.assertEqual(
            self.client.usage(self.output(secondary=absent)), Usage(FIVE, Window(41, None))
        )

    def test_invalid_percent_or_duration_drops_only_its_window(self):
        for value in (None, True, "62", -1, float("inf"), float("nan"), 10**1000):
            with self.subTest(value=value):
                self.assertEqual(
                    self.client.usage(self.output(primary={**self.five, "usedPercent": value})),
                    Usage(None, WEEK),
                )
                self.assertEqual(
                    self.client.usage(self.output(secondary={**self.weekly, "usedPercent": value})),
                    Usage(FIVE, None),
                )
        absent = {key: value for key, value in self.five.items() if key != "usedPercent"}
        self.assertEqual(self.client.usage(self.output(primary=absent)), Usage(None, WEEK))
        for duration in (300.0, "300", True, None):
            with self.subTest(duration=duration):
                self.assertEqual(
                    self.client.usage(
                        self.output(primary={**self.five, "windowDurationMins": duration})
                    ),
                    Usage(None, WEEK),
                )

    def test_the_first_window_of_a_duration_wins(self):
        later = {**self.five, "usedPercent": 10, "resetsAt": 3000}
        self.assertEqual(self.client.usage(self.output(self.five, later)), Usage(FIVE, None))
        self.assertEqual(
            self.client.usage(self.output({**self.five, "usedPercent": None}, later)),
            Usage(None, None),
        )

    def test_output_without_a_snapshot_is_a_failed_probe(self):
        for output in (
            "",
            "null",
            "{}",
            "[]",
            '"text"',
            "{bad json",
            "[" * 2000,
            json.dumps({"rateLimits": None}),
            json.dumps({"rateLimits": [self.five]}),
            json.dumps({"rateLimitsByLimitId": {"codex": None}}),
            json.dumps({"rateLimitsByLimitId": "codex", "rateLimits": "bad"}),
        ):
            with self.subTest(output=output[:80]):
                self.assertIsNone(self.client.usage(output))


class ConfigurationPolicy(Base):
    def test_client_has_no_policy_configuration(self):
        for key in ("sandbox", "shell_network"):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, "unknown key"):
                CodexClient(self.client.config | {key: "workspace-write"})


class InstallationTest(Base):
    def test_other_client_scope_is_identity_without_writes(self):
        launch = Launch(("client",), env={"A": "B"})
        with Client({})._launch_scope(self.spec, launch) as (actual, relocate):
            self.assertIs(actual, launch)
            self.assertEqual(relocate({"payload": 1}), {"payload": 1})
        self.assertFalse(Path(installations_root()).exists())

    def test_preflight_rejects_writable_snapshot_root_and_symlink_aliases(self):
        root = Path(installations_root())
        root.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir(mode=0o700)
        alias = self.root / "alias"
        alias.symlink_to(root, target_is_directory=True)
        for directory in (root, root.parent, root / "child", alias):
            with self.subTest(directory=directory):
                spec = replace(self.spec, access=replace(self.spec.access, dirs=(str(directory),)))
                with self.assertRaisesRegex(
                    ConfigError, "(retained installation|recovery state).*writable"
                ):
                    self.common.prepare(spec)
        self.assertEqual(list(root.iterdir()), [])

    def test_cwd_codex_home_and_add_dir_overlap_are_rejected(self):
        root = installations_root()
        specs = (
            replace(self.spec, access=Access(cwd=root)),
            replace(self.spec, flags=("--add-dir", root)),
            replace(self.spec, env={"CODEX_HOME": root}),
        )
        for spec in specs:
            with (
                self.subTest(spec=spec),
                self.assertRaisesRegex(ConfigError, "retained installation"),
            ):
                self.common.prepare(spec)
        self.assertFalse(Path(root).exists())

    def test_scope_relocates_encoded_hook_and_callback_roots_and_cleans_failed_start(self):
        source = Path(REPO)
        hook = str(source / "bin/agent-pm-session")
        launch = Launch(
            ("python3", "-m", "start", json.dumps({"hook": [hook]})),
            env={"PATH": str(source / "bin")},
        )
        with (
            self.assertRaisesRegex(RuntimeError, "failed start"),
            self.client._launch_scope(self.spec, launch) as (kept, relocate),
        ):
            encoded = json.loads(kept.argv[-1])[-1]
            copy = next(Path(installations_root()).iterdir())
            self.assertEqual(json.loads(encoded)["hook"], [str(copy / "bin/agent-pm-session")])
            self.assertEqual(
                relocate({"roots": [str(source / "utils/src")]}),
                {"roots": [str(copy / "utils/src")]},
            )
            self.assertTrue(kept.env["PATH"].startswith(str(copy / "bin")))
            raise RuntimeError("failed start")
        self.assertEqual(list(Path(installations_root()).iterdir()), [])

    def test_non_plugin_installs_preserve_launch_and_preflight_creates_no_copy(self):
        self.common.prepare(self.spec)
        self.assertFalse(Path(installations_root()).exists())
        launch = Launch(("client",))
        with (
            mock.patch("os.path.isfile", return_value=False),
            self.client._launch_scope(self.spec, launch) as (actual, _map),
        ):
            self.assertIs(actual, launch)
