from new.tests.unit.adapters.codex.support import NATIVE, Base

import dataclasses
import json
import sys
from unittest import mock

from adapters.base import ConfigError
from adapters.codex import CodexAdapter

HOOK = (sys.executable, "-I", "/protected/driver_hook.py", "context")


class SandboxTest(Base):
    def argv(self, adapter, mode, spec, resume=False):
        if mode == "headless":
            return list(adapter.headless().launch("hi", spec, resume).argv)
        with mock.patch("shutil.which", return_value="/protected/codex"):
            launch = adapter.tui().launch("hi", spec, resume, HOOK)
        return json.loads(launch.argv[-1])["argv"]

    def assert_policy(self, argv, sandbox, network=False):
        expected = [
            "-s",
            sandbox,
            "-c",
            'approval_policy="on-request"',
            "-c",
            'approvals_reviewer="auto_review"',
        ]
        if sandbox == "workspace-write":
            expected.extend(
                [
                    "-c",
                    f"sandbox_workspace_write.network_access={str(network).lower()}",
                    "-c",
                    "sandbox_workspace_write.writable_roots=[]",
                ]
            )
        expected.extend(
            [
                "-m",
                "gpt-test",
                "-c",
                "model_reasoning_effort=low",
                "-c",
                "project_doc_max_bytes=0",
            ]
        )
        if sandbox == "workspace-write":
            expected.extend(["--add-dir", str(self.extra)])
        start = argv.index("-s")
        self.assertEqual(argv[start : start + len(expected)], expected)
        self.assertEqual(argv.count("-s"), 1)
        self.assertEqual(argv.count('approval_policy="on-request"'), 1)
        self.assertEqual(argv.count('approvals_reviewer="auto_review"'), 1)
        self.assertNotIn("--approve-for-me", argv)
        if sandbox == "danger-full-access":
            self.assertNotIn("--add-dir", argv)
            self.assertFalse(any(word.startswith("sandbox_workspace_write.") for word in argv))

    def test_default_and_explicit_policy_in_both_modes(self):
        for mode in ("headless", "tui"):
            for sandbox in (None, "danger-full-access", "workspace-write"):
                for network in (False, True):
                    with self.subTest(mode=mode, sandbox=sandbox, network=network):
                        cfg = self.adapter.config | {"shell_network": network}
                        if sandbox is not None:
                            cfg["sandbox"] = sandbox
                        adapter = CodexAdapter(cfg)
                        spec = dataclasses.replace(self.spec, sid=f"{mode}-{sandbox}-{network}")
                        argv = self.argv(adapter, mode, spec)
                        effective = sandbox or "danger-full-access"
                        self.assert_policy(argv, effective, network)
                        self.assertEqual(
                            adapter.common.load(str(self.work), spec.sid).snapshot["sandbox"],
                            effective,
                        )

    def test_invalid_config_policy(self):
        for value in (None, True, 0, [], {}, "", "read-only", "danger-full-access "):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ConfigError, "sandbox.*danger-full-access.*workspace-write"),
            ):
                CodexAdapter(self.adapter.config | {"sandbox": value})

    def test_resume_keeps_saved_policy_in_both_directions_and_modes(self):
        for mode in ("headless", "tui"):
            for saved, current in (
                ("workspace-write", "danger-full-access"),
                ("danger-full-access", "workspace-write"),
            ):
                with self.subTest(mode=mode, saved=saved):
                    spec = dataclasses.replace(self.spec, sid=f"{mode}-{saved}")
                    original = CodexAdapter(self.adapter.config | {"sandbox": saved})
                    self.argv(original, mode, spec)
                    original.common.bind(str(self.work), spec.sid, NATIVE)
                    self.rollout()
                    adapter = CodexAdapter(
                        self.adapter.config | {"sandbox": current, "shell_network": True}
                    )
                    argv = self.argv(adapter, mode, spec, True)
                    self.assert_policy(argv, saved)
                    self.assertEqual(
                        adapter.common.load(str(self.work), spec.sid).snapshot["sandbox"], saved
                    )

    def test_legacy_snapshot_resumes_workspace_write_in_both_modes(self):
        state = self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        snapshot = state.snapshot.copy()
        snapshot.pop("sandbox", None)
        self.edit_record(snapshot=snapshot)
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                self.assert_policy(
                    self.argv(self.adapter, mode, self.spec, True), "workspace-write"
                )
                self.assertEqual(
                    self.common.load(str(self.work), self.spec.sid).snapshot["sandbox"],
                    "workspace-write",
                )

    def test_invalid_saved_policy_fails_closed(self):
        state = self.prepare()
        for value in (None, True, 0, [], {}, "", "read-only"):
            self.edit_record(snapshot=state.snapshot | {"sandbox": value})
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "sandbox"):
                self.common.load(str(self.work), self.spec.sid)
