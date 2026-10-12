import io
import json
import contextlib
import os
import time
from unittest import mock

from adapters.codex import tui_hook
from new.tests.unit.adapters.codex.support import Base, CHILD, NATIVE


class HookTest(Base):
    def payload(self, **fields):
        return {"session_id": NATIVE, "cwd": str(self.cwd), **fields}

    def test_bind_before_forward_and_ignore_children_foreign_or_rebound(self):
        self.prepare()
        self.assertTrue(tui_hook.bind("SessionStart", self.payload(), str(self.work), self.spec.sid))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)
        for fields in ({"agent_id": CHILD}, {"cwd": str(self.extra)}, {"session_id": CHILD},
                       {"session_id": "invalid"}, {"hook_event_name": "PermissionRequest"}):
            with self.subTest(fields=fields):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertFalse(tui_hook.bind("Stop", self.payload(**fields), str(self.work), self.spec.sid))
        self.assertFalse(tui_hook.bind("unknown", self.payload(), str(self.work), self.spec.sid))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_payload_drains_oversized_input_and_rejects_non_objects(self):
        for value in (None, [], "bad"):
            self.assertEqual(tui_hook.payload(io.BytesIO(json.dumps(value).encode())), {})
        stream = io.BytesIO(b"x" * (tui_hook.PAYLOAD_MAX + 10))
        self.assertEqual(tui_hook.payload(stream), {})
        self.assertEqual(stream.read(), b"")
        self.assertEqual(tui_hook.payload(io.BytesIO(json.dumps(self.payload()).encode())), self.payload())

    def test_main_is_silent_and_does_not_publish_invalid_binding(self):
        self.prepare()
        stdin = mock.Mock(buffer=io.BytesIO(json.dumps(self.payload(agent_id=CHILD)).encode()))
        with mock.patch.object(tui_hook.sys, "stdin", stdin), mock.patch.object(tui_hook, "forward") as forward:
            tui_hook.main([str(self.work), self.spec.sid, json.dumps(["/protected/python", "hook.py"]), "Stop"])
        forward.assert_not_called()
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)

    def test_forward_passes_seekable_stdin_and_opaque_exact_argv(self):
        with mock.patch("os.dup2") as dup, mock.patch("os.execv") as execute:
            tui_hook.forward(["/protected/python", "-I", "/protected/hook.py", "context"], "Stop", self.payload())
        dup.assert_called_once()
        execute.assert_called_once_with("/protected/python", ["/protected/python", "-I", "/protected/hook.py", "context", "Stop"])

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
        with mock.patch("fcntl.flock", side_effect=[BlockingIOError, None]), \
                mock.patch("time.monotonic", side_effect=[0, 2]), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(tui_hook.bind("Interrupt", self.payload(), str(self.work), self.spec.sid))
        self.assertIsNone(self.common.load(str(self.work), self.spec.sid).native_id)
