from new.tests.unit.adapters.codex.support import CHILD, NATIVE, Base

import dataclasses
import os
import shlex
import stat
import time
from unittest import mock

from adapters.base import Access, ConfigError
from adapters.codex import CodexAdapter


class CommonTest(Base):
    def test_durable_snapshot_and_first_binding(self):
        with mock.patch("os.fsync", wraps=os.fsync) as sync:
            state = self.prepare()
        self.assertGreaterEqual(sync.call_count, 2)
        self.assertEqual(state.snapshot["cwd"], str(self.cwd))
        self.assertIsNone(state.native_id)
        path = self.record()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        with mock.patch("os.fsync", wraps=os.fsync) as sync:
            bound = self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.assertGreaterEqual(sync.call_count, 2)
        self.assertEqual(self.common.load(str(self.work), self.spec.sid), bound)
        self.assertEqual(self.common.bind(str(self.work), self.spec.sid, NATIVE), bound)
        with self.assertRaisesRegex(ConfigError, "bound|different"):
            self.common.bind(str(self.work), self.spec.sid, CHILD)
        with self.assertRaisesRegex(ConfigError, "already exists"):
            self.prepare()
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_bind_rejects_missing_or_invalid_native_id(self):
        self.prepare()
        for value in (None, "invalid", "11111111-2222-3333-4444-55555555555X"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "native ID"):
                self.common.bind(str(self.work), self.spec.sid, value)

    def test_resume_uses_saved_effective_settings(self):
        saved = self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        other = CodexAdapter(
            {
                "tiers": {"1": "changed"},
                "efforts": {"low": "high"},
                "shell_network": True,
                "env": {"CODEX_HOME": str(self.root / "elsewhere")},
            }
        )
        spec = dataclasses.replace(self.spec, access=Access(project=True), tier=None, effort=None)
        resumed = other.common.prepare(spec, True)
        self.assertEqual(resumed.snapshot, saved.snapshot)
        launch = other.headless().launch("again", spec, True)
        self.assertIn("gpt-test", launch.argv)
        self.assertIn("model_reasoning_effort=low", launch.argv)
        self.assertEqual(launch.env["CODEX_HOME"], str(self.codex_home))
        self.assertEqual(launch.cwd, str(self.cwd))
        self.assertIn(NATIVE, launch.argv)

    def test_omitted_mapping_preserves_runtime_defaults(self):
        spec = dataclasses.replace(self.spec, tier=None, effort=None)
        launch = self.adapter.headless().launch("hi", spec, False)
        self.assertNotIn("-m", launch.argv)
        self.assertFalse(any(x.startswith("model_reasoning_effort=") for x in launch.argv))
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).snapshot["model"])

    def test_missing_recovery_and_session_fail_before_launch(self):
        with self.assertRaisesRegex(ConfigError, "missing.*state"):
            self.common.prepare(self.spec, True)
        self.prepare()
        with self.assertRaisesRegex(ConfigError, "missing.*ID"):
            self.common.prepare(self.spec, True)
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        with self.assertRaisesRegex(ConfigError, "missing.*file"):
            self.common.prepare(self.spec, True)

    def test_fifo_record_is_nonblocking(self):
        self.prepare()
        path = self.record()
        path.unlink()
        os.mkfifo(path, 0o600)
        before = time.monotonic()
        with self.assertRaisesRegex(ConfigError, "unsafe"):
            self.common.prepare(self.spec, True)
        self.assertLess(time.monotonic() - before, 0.5)

    def test_record_type_mode_owner_size_and_json_guards(self):
        self.prepare()
        path = self.record()
        original = path.read_text()
        for content in ("not json", "[" * 2000, "x" * (64 * 1024 + 1)):
            with self.subTest(content=content[:10]):
                path.write_text(content)
                with self.assertRaisesRegex(ConfigError, "malformed"):
                    self.common.load(str(self.work), self.spec.sid)
        path.write_text(original)
        path.chmod(0o644)
        with self.assertRaisesRegex(ConfigError, "mode|owner"):
            self.common.load(str(self.work), self.spec.sid)
        path.chmod(0o600)
        with (
            mock.patch("os.getuid", return_value=os.getuid() + 1),
            self.assertRaisesRegex(ConfigError, "owner"),
        ):
            self.common.load(str(self.work), self.spec.sid)
        path.unlink()
        path.symlink_to(self.root / "absent")
        with self.assertRaisesRegex(ConfigError, "symlink"):
            self.common.load(str(self.work), self.spec.sid)

    def test_record_identity_snapshot_and_grants_are_revalidated(self):
        state = self.prepare()
        original = self.record().read_text()
        cases = (
            {"sid": "forged"},
            {"snapshot": {**state.snapshot, "network": "yes"}},
            {"snapshot": {**state.snapshot, "cwd": str(self.work / ".." / "work")}},
            {"writable_roots": [str(self.extra)]},
            {"writable_roots": [str(self.home)]},
            {"native_id": "not-a-uuid"},
        )
        for fields in cases:
            with self.subTest(fields=fields):
                self.record().write_text(original)
                self.edit_record(**fields)
                with self.assertRaises(ConfigError):
                    self.common.load(str(self.work), self.spec.sid)

    def test_store_outside_explicit_and_implicit_writable_roots(self):
        for grant in (self.home, self.home / ".agent-pm", self.root):
            with self.subTest(grant=grant), self.assertRaisesRegex(ConfigError, "writable"):
                self.common.prepare(
                    dataclasses.replace(self.spec, access=Access(dirs=(str(grant),))), False
                )
        with mock.patch.dict(os.environ, {"HOME": "/tmp/agent-pm-untrusted-home"}):
            with self.assertRaisesRegex(ConfigError, "writable"):
                self.common.load(str(self.work), self.spec.sid)

    def test_configured_tmpdir_cannot_grant_store_on_fresh_prepare(self):
        adapter = CodexAdapter(self.adapter.config | {"env": {"TMPDIR": str(self.home)}})
        with self.assertRaisesRegex(ConfigError, "writable"):
            adapter.common.prepare(self.spec, False)
        self.assertFalse((self.home / ".agent-pm").exists())

    def test_configured_tmpdir_cannot_grant_store_on_resumed_prepare(self):
        self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        self.rollout()
        saved = self.record().read_bytes()
        adapter = CodexAdapter(self.adapter.config | {"env": {"TMPDIR": str(self.home)}})
        with self.assertRaisesRegex(ConfigError, "writable"):
            adapter.common.prepare(self.spec, True)
        self.assertEqual(self.record().read_bytes(), saved)

    def test_store_parent_permissions_and_symlink_guard(self):
        parent = self.home / ".agent-pm"
        parent.mkdir(mode=0o755)
        self.prepare()
        parent.chmod(0o775)
        with self.assertRaisesRegex(ConfigError, "mode|owner"):
            self.common.load(str(self.work), self.spec.sid)

    def test_store_parent_symlink_fails_before_creation(self):
        target = self.root / "store-target"
        target.mkdir()
        (self.home / ".agent-pm").symlink_to(target)
        with self.assertRaisesRegex(ConfigError, "symlink"):
            self.prepare()
        self.assertEqual(list(target.iterdir()), [])

    def test_untrusted_project_configuration_and_cwd(self):
        settings = self.cwd / ".codex"
        settings.mkdir()
        with self.assertRaisesRegex(ConfigError, "project.*settings"):
            self.prepare()
        settings.rmdir()
        with self.assertRaisesRegex(ConfigError, "outside"):
            self.common.prepare(
                dataclasses.replace(self.spec, access=Access(cwd=str(self.extra))), False
            )

    def test_config_validation_and_environment_isolation(self):
        for cfg in (
            {"shell_network": "yes"},
            {"tiers": {"1": "\n"}},
            {"efforts": []},
            {"env": {"A": 1}},
        ):
            with self.subTest(cfg=cfg), self.assertRaises(ConfigError):
                CodexAdapter({"tiers": {}, "efforts": {}, "shell_network": False, **cfg})
        adapter = CodexAdapter(
            {"tiers": {}, "efforts": {}, "shell_network": False, "env": {"PROBE": "1"}}
        )
        launch = adapter.headless().launch(
            "hi", dataclasses.replace(self.spec, tier=None, effort=None), False
        )
        self.assertEqual(launch.env["PROBE"], "1")
        self.assertEqual(launch.env["NO_COLOR"], "1")
        self.assertIn("CODEX_THREAD_ID", launch.unset)
        self.assertIn("CODEX_PERMISSION_PROFILE", launch.unset)
        self.assertIn("sandbox_workspace_write.writable_roots=[]", launch.argv)
        self.assertIsNotNone(adapter.tui())

    def test_missing_mapping_is_configuration_error(self):
        with self.assertRaisesRegex(ConfigError, "model|tier"):
            CodexAdapter({"tiers": {}, "efforts": {}, "shell_network": False}).headless().launch(
                "hi", self.spec, False
            )


class InspectionTest(Base):
    def bound(self):
        state = self.prepare()
        self.common.bind(str(self.work), self.spec.sid, NATIVE)
        return state

    def test_inspection_queries_saved_cwd_in_one_store(self):
        self.bound()
        path = self.rollout()
        info = self.common.inspect(self.spec.sid, str(self.cwd))
        self.assertEqual(
            (info.resumable, info.native_id, info.transcript, info.last_active),
            ("yes", NATIVE, str(path), 100),
        )
        command = shlex.split(info.resume)
        self.assertIn(NATIVE, command)
        self.assertIn(str(self.cwd), command)
        self.assertEqual(self.common.inspect(self.spec.sid, str(self.work)).resumable, "no")
        path.unlink()
        self.assertEqual(self.common.inspect(self.spec.sid, str(self.cwd)).resumable, "no")

    def test_inspection_missing_malformed_unsafe_and_ambiguous(self):
        self.assertEqual(self.common.inspect(self.spec.sid, str(self.cwd)).resumable, "no")
        self.bound()
        self.rollout()
        path = self.record()
        path.write_text("not json")
        self.assertEqual(self.common.inspect(self.spec.sid, str(self.cwd)).resumable, "unknown")

    def test_unreadable_store_and_session_are_unknown(self):
        self.bound()
        self.rollout()
        with mock.patch("os.scandir", side_effect=PermissionError("denied")):
            self.assertEqual(self.common.inspect(self.spec.sid, str(self.cwd)).resumable, "unknown")
        path = self.rollout()
        original = os.open

        def opening(location, *args, **kwargs):
            if str(location) == str(path):
                raise PermissionError("denied")
            return original(location, *args, **kwargs)

        with mock.patch("os.open", side_effect=opening):
            self.assertEqual(self.common.inspect(self.spec.sid, str(self.cwd)).resumable, "unknown")

    def test_transcript_fifo_mismatch_duplicate_and_symlink(self):
        self.bound()
        path = self.rollout()
        path.unlink()
        os.mkfifo(path)
        before = time.monotonic()
        with self.assertRaisesRegex(ConfigError, "unsafe"):
            self.common.prepare(self.spec, True)
        self.assertLess(time.monotonic() - before, 0.5)
        path.unlink()
        path = self.rollout(cwd=str(self.extra))
        with self.assertRaisesRegex(ConfigError, "mismatched"):
            self.common.prepare(self.spec, True)
        path = self.rollout()
        duplicate = path.with_name("rollout-other-" + NATIVE + ".jsonl")
        duplicate.write_text(path.read_text())
        with self.assertRaisesRegex(ConfigError, "duplicate"):
            self.common.prepare(self.spec, True)
        duplicate.unlink()
        path.unlink()
        path.symlink_to(self.root / "absent")
        self.assertEqual(self.common.inspect(self.spec.sid, str(self.cwd)).resumable, "unknown")

    def test_last_activity_includes_validated_descendants(self):
        self.bound()
        self.rollout(stamp=100)
        self.rollout(CHILD, parent=NATIVE, stamp=200)
        self.rollout("bbbbbbbb-2222-3333-4444-555555555555", parent=CHILD, stamp=300)
        self.rollout("cccccccc-2222-3333-4444-555555555555", stamp=999)
        self.assertEqual(self.common.inspect(self.spec.sid, str(self.cwd)).last_active, 300)

    def test_duplicate_descendant_is_not_validated_activity(self):
        self.bound()
        self.rollout()
        child = self.rollout(CHILD, parent=NATIVE, stamp=200)
        duplicate = child.with_name("rollout-copy-" + CHILD + ".jsonl")
        duplicate.write_text(child.read_text())
        self.assertEqual(self.common.inspect(self.spec.sid, str(self.cwd)).resumable, "unknown")

    def test_ambiguous_sid_and_cwd_is_unknown(self):
        self.bound()
        self.rollout()
        other = self.root / "other"
        other.mkdir()
        self.common.prepare(
            dataclasses.replace(
                self.spec, workdir=str(other), access=Access(cwd=str(self.cwd), project=True)
            ),
            False,
        )
        self.assertEqual(self.common.inspect(self.spec.sid, str(self.cwd)).resumable, "unknown")
