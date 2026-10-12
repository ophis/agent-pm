"""Codex standalone hook binding and opaque Driver forwarding."""

import contextlib
import io
import json
import os
import subprocess
import sys
import time
from unittest import mock

from agent_pm.adapters.codex import sessions, tui_hook

from new.tests.unit.adapters.codex.support import CHILD, NATIVE, Base


class HookTest(Base):
    def payload(self, **fields):
        return {"session_id": NATIVE, "cwd": str(self.cwd), **fields}

    def test_bind_before_forward_and_ignore_children_foreign_or_rebound(self):
        self.prepare()
        self.assertTrue(
            tui_hook.bind("SessionStart", self.payload(), str(self.work), self.spec.sid)
        )
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        for fields in (
            {"agent_id": CHILD},
            {"cwd": str(self.extra)},
            {"session_id": CHILD},
            {"session_id": "invalid"},
            {"hook_event_name": "PermissionRequest"},
        ):
            with self.subTest(fields=fields):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertFalse(
                        tui_hook.bind("Stop", self.payload(**fields), str(self.work), self.spec.sid)
                    )
        self.assertFalse(tui_hook.bind("unknown", self.payload(), str(self.work), self.spec.sid))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_run_by_path_isolated_it_loads_its_siblings_and_refuses_bad_arguments(self):
        result = subprocess.run(
            [sys.executable, "-I", tui_hook.__file__], input="", capture_output=True, text=True
        )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (0, "", "codex hook: expected WORKDIR SID STORE HOOK_JSON LABEL\n"),
        )

    def test_payload_drains_oversized_input_and_rejects_non_objects(self):
        for value in (None, [], "bad"):
            self.assertEqual(tui_hook.payload(io.BytesIO(json.dumps(value).encode())), {})
        stream = io.BytesIO(b"x" * (tui_hook.PAYLOAD_MAX + 10))
        self.assertEqual(tui_hook.payload(stream), {})
        self.assertEqual(stream.read(), b"")
        self.assertEqual(
            tui_hook.payload(io.BytesIO(json.dumps(self.payload()).encode())), self.payload()
        )

    def test_main_is_silent_and_does_not_publish_invalid_binding(self):
        self.prepare()
        stdin = mock.Mock(buffer=io.BytesIO(json.dumps(self.payload(agent_id=CHILD)).encode()))
        with (
            mock.patch.object(tui_hook.sys, "stdin", stdin),
            mock.patch.object(tui_hook, "forward") as forward,
        ):
            tui_hook.main(
                [
                    str(self.work),
                    self.spec.sid,
                    sessions.store_root(),
                    json.dumps(["/protected/python", "hook.py"]),
                    "Stop",
                ]
            )
        forward.assert_not_called()
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)

    def test_forward_passes_seekable_stdin_and_opaque_exact_argv(self):
        with mock.patch("os.dup2") as dup, mock.patch("os.execv") as execute:
            tui_hook.forward(
                ["/protected/python", "-I", "/protected/hook.py", "context"], "Stop", self.payload()
            )
        dup.assert_called_once()
        execute.assert_called_once_with(
            "/protected/python",
            ["/protected/python", "-I", "/protected/hook.py", "context", "Stop"],
        )

    def test_missing_malformed_and_fifo_record_fail_without_forwarding(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(tui_hook.bind("Stop", self.payload(), str(self.work), self.spec.sid))
        self.prepare()
        path = self.record()
        path.write_text("not-json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(tui_hook.bind("Stop", self.payload(), str(self.work), self.spec.sid))
        path.unlink()
        os.mkfifo(path, 0o600)
        before = time.monotonic()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(tui_hook.bind("Stop", self.payload(), str(self.work), self.spec.sid))
        self.assertLess(time.monotonic() - before, 0.5)

    def test_binding_lock_wait_is_bounded(self):
        self.prepare()
        with (
            mock.patch("fcntl.flock", side_effect=[BlockingIOError, None]),
            mock.patch("time.monotonic", side_effect=[0, 2]),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertFalse(
                tui_hook.bind("Interrupt", self.payload(), str(self.work), self.spec.sid)
            )
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)

    def test_explicit_caller_store_survives_configured_runtime_home(self):
        self.prepare()
        store = sessions.store_root()
        runtime_home = self.root / "runtime-home"
        runtime_home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(runtime_home)}):
            self.assertTrue(
                tui_hook.bind("Stop", self.payload(), str(self.work), self.spec.sid, root=store)
            )
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        self.assertFalse((runtime_home / ".agent-pm").exists())

    def test_explicit_store_keeps_symlink_mode_and_implicit_root_guards(self):
        self.prepare()
        store = self.record().parent
        alias = self.root / "alias"
        alias.symlink_to(store, target_is_directory=True)
        for path in (str(alias), "relative", str(store)):
            with self.subTest(path=path):
                if path == str(store):
                    store.chmod(0o755)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertFalse(
                        tui_hook.bind(
                            "Stop", self.payload(), str(self.work), self.spec.sid, root=path
                        )
                    )
                store.chmod(0o700)
        with (
            mock.patch.dict(os.environ, {"TMPDIR": str(store.parent)}),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertFalse(
                tui_hook.bind(
                    "Stop", self.payload(), str(self.work), self.spec.sid, root=str(store)
                )
            )
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)

    def test_authoritative_store_validates_every_recovery_path_with_writable_runtime_home(self):
        state = self.prepare()
        record = self.record()
        store = str(record.parent)
        runtime_home = self.cwd / "runtime-home"
        runtime_home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(runtime_home)}):
            self.assertEqual(sessions.recovery_snapshot(state, root=store), state.snapshot)
            self.assertEqual(sessions.read_state(str(record), root=store), state)
            self.assertEqual(
                sessions.load_session(str(self.work), self.spec.sid, root=store), state
            )
            bound = sessions.bind_session(str(self.work), self.spec.sid, NATIVE, root=store)
            self.assertEqual(bound.native_id, NATIVE)
            self.assertTrue(
                tui_hook.bind("Stop", self.payload(), str(self.work), self.spec.sid, root=store)
            )
            fresh = sessions.create_session(
                str(self.work),
                "explicit-store",
                state.snapshot,
                state.writable_roots,
                persist=False,
                root=store,
            )
            self.assertEqual(sessions.save_session(fresh, root=store), fresh)
            self.assertEqual(sessions.load_session(str(self.work), fresh.sid, root=store), fresh)
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        self.assertFalse((runtime_home / ".agent-pm").exists())

    def test_authoritative_store_still_rejects_actual_writable_store(self):
        state = self.prepare()
        record = self.record()
        unsafe = self.cwd / ".private-state" / "core-new-sessions" / "codex"
        for directory in (unsafe.parent.parent, unsafe.parent, unsafe):
            directory.mkdir(mode=0o700, exist_ok=True)
        copied = unsafe / record.name
        copied.write_bytes(record.read_bytes())
        copied.chmod(0o600)
        root = str(unsafe)
        calls = (
            lambda: sessions.recovery_snapshot(state, root=root),
            lambda: sessions.read_state(str(copied), root=root),
            lambda: sessions.load_session(str(self.work), self.spec.sid, root=root),
            lambda: sessions.bind_session(str(self.work), self.spec.sid, NATIVE, root=root),
            lambda: sessions.create_session(
                str(self.work), "unsafe-store", state.snapshot, state.writable_roots, root=root
            ),
            lambda: sessions.save_session(state, root=root),
        )
        for call in calls:
            with (
                self.subTest(call=call),
                self.assertRaisesRegex(sessions.RecoveryError, "inside writable root"),
            ):
                call()
        self.assertEqual(record.read_bytes(), copied.read_bytes())
        self.assertEqual(list(unsafe.iterdir()), [copied])
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)
