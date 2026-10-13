"""Codex standalone hook binding and opaque Driver forwarding."""

import contextlib
import io
import json
import os
import subprocess
import sys
import time
from unittest import mock

from agent_pm.session.adapters.codex import sessions, tui_hook

from session.tests.unit.adapters.codex.support import CHILD, NATIVE, Base


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
            with self.subTest(fields=fields), contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(
                    tui_hook.bind("Stop", self.payload(**fields), str(self.work), self.spec.sid)
                )
        self.assertFalse(tui_hook.bind("unknown", self.payload(), str(self.work), self.spec.sid))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_callback_binds_and_adds_label_without_another_payload_reader(self):
        self.prepare()
        payload = self.payload(last_assistant_message="reply")
        state = {
            "workdir": str(self.work),
            "sid": self.spec.sid,
            "adapter_state": {"store": sessions.SessionStore().root},
        }
        self.assertTrue(tui_hook.on_hook("Stop", payload, state))
        self.assertEqual(payload["hook_event_name"], "Stop")
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        self.assertFalse(tui_hook.on_hook("Stop", self.payload(agent_id=CHILD), state))

    def test_generic_isolated_hook_loads_binding_callback_and_records_once(self):
        self.prepare()
        folder = self.root / "hook-session"
        folder.mkdir()
        from agent_pm.session.modes import tui_hook as driver_hook

        state = {
            "runtime": "codex",
            "sid": self.spec.sid,
            "workdir": str(self.work),
            "cwd": str(self.cwd),
            "project": False,
            "transcript": "",
            "resume": "",
            "events": None,
            "words": {},
            "tmux_path": sys.executable,
            "adapter_hook": tui_hook.__file__,
            "adapter_state": {"store": sessions.SessionStore().root},
        }
        (folder / "session.json").write_text(json.dumps(state))
        result = subprocess.run(
            [sys.executable, "-I", driver_hook.__file__, str(folder), "Stop"],
            input=json.dumps(self.payload(last_assistant_message="reply")),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        records = [json.loads(line) for line in (folder / "hooks.jsonl").read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["payload"]["hook_event_name"], "Stop")
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        result = subprocess.run(
            [sys.executable, "-I", driver_hook.__file__, str(folder), "Stop"],
            input=json.dumps(self.payload(agent_id=CHILD)),
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(len((folder / "hooks.jsonl").read_text().splitlines()), 1)

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
        store = sessions.SessionStore().root
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
            self.assertEqual(sessions.SessionStore(store).validate_recovery(state), state.snapshot)
            self.assertEqual(
                sessions.SessionStore(store).lookup(self.spec.sid, state.snapshot["cwd"]), state
            )
            self.assertEqual(
                sessions.SessionStore(store).load(str(self.work), self.spec.sid), state
            )
            bound = sessions.SessionStore(store).bind(str(self.work), self.spec.sid, NATIVE)
            self.assertEqual(bound.native_id, NATIVE)
            self.assertTrue(
                tui_hook.bind("Stop", self.payload(), str(self.work), self.spec.sid, root=store)
            )
            fresh = sessions.SessionStore(store).create(
                str(self.work),
                "explicit-store",
                state.snapshot,
                state.writable_roots,
                persist=False,
            )
            self.assertEqual(sessions.SessionStore(store).save(fresh), fresh)
            self.assertEqual(sessions.SessionStore(store).load(str(self.work), fresh.sid), fresh)
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
            lambda: sessions.SessionStore(root).validate_recovery(state),
            lambda: sessions.SessionStore(root).lookup(self.spec.sid, state.snapshot["cwd"]),
            lambda: sessions.SessionStore(root).load(str(self.work), self.spec.sid),
            lambda: sessions.SessionStore(root).bind(str(self.work), self.spec.sid, NATIVE),
            lambda: sessions.SessionStore(root).create(
                str(self.work), "unsafe-store", state.snapshot, state.writable_roots
            ),
            lambda: sessions.SessionStore(root).save(state),
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
