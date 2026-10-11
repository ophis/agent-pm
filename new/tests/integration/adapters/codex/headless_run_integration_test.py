import dataclasses
import json
import os
import shlex
import sys
import threading
from unittest import mock

from adapters.base import ConfigError, Exited, LimitHit, Reply, SessionBound, TurnEnd
from driver import api, registry
from new.tests.integration.adapters.codex import fake_codex
from new.tests.unit.adapters.codex.support import Base, NATIVE


class HeadlessIntegrationTest(Base):
    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        executable = self.bin / "codex"
        executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                              + shlex.quote(fake_codex.__file__) + ' "$@"\n')
        executable.chmod(0o755)
        self.log = self.root / "exec.jsonl"
        cfg = self.adapter.config | {"env": {"FAKE_LOG": str(self.log)}}
        self.config = {"adapters": {"codex": {"implementation": "adapters.codex:CodexAdapter", **cfg}}}
        self.enterContext(mock.patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                                                      "CODEX_THREAD_ID": "inherited"}))
        self.enterContext(mock.patch.object(registry, "read_config", return_value=self.config))

    def drain(self, handle):
        self.addCleanup(handle.stop)
        found, errors = [], []

        def collect():
            try:
                found.extend(handle.events())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=collect, daemon=True)
        thread.start()
        thread.join(10)
        if thread.is_alive():
            handle.stop()
            self.fail("Driver did not reach Exited within 10s")
        if errors:
            raise errors[0]
        return found

    def logged(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_driver_replies_native_sid_and_snapshot(self):
        handle = api.start("codex", "headless", "hello", self.spec)
        self.assertEqual(self.drain(handle), [SessionBound(NATIVE), Reply("one\n\nnext line\n"), Reply("two"),
                                             Reply("three"), TurnEnd("three"), Exited(0)])
        self.assertEqual(handle.sid, self.spec.sid)
        self.assertEqual(handle.info().native_id, NATIVE)
        self.assertEqual(handle.reply, "three")
        entry, = self.logged()
        self.assertEqual(entry["snapshot"]["sid"], self.spec.sid)
        self.assertIsNone(entry["snapshot"]["native_id"])
        self.assertIsNone(entry["marker"])
        self.assertTrue(entry["argv"][-1].endswith(self.adapter.runtime_note))
        self.assertEqual(self.common.load(str(self.work), self.spec.sid).native_id, NATIVE)

    def test_resume_same_native_and_saved_settings(self):
        self.drain(api.start("codex", "headless", "one", self.spec))
        self.config["adapters"]["codex"]["tiers"] = {"1": "changed"}
        spec = dataclasses.replace(self.spec, tier=None, effort=None)
        handle = api.start("codex", "headless", "again", spec, resume=self.spec.sid)
        self.assertEqual(self.drain(handle)[0], SessionBound(NATIVE))
        first, second = self.logged()
        self.assertEqual(second["snapshot"]["snapshot"], first["snapshot"]["snapshot"])
        self.assertIn("gpt-test", second["argv"])
        self.assertIn(NATIVE, second["argv"])

    def test_missing_binding_record_and_fifo_do_not_spawn(self):
        self.drain(api.start("codex", "headless", "one", self.spec))
        path = self.record()
        original = path.read_text()
        for mode in ("unbound", "missing", "fifo", "malformed"):
            with self.subTest(mode=mode):
                if path.exists():
                    path.unlink()
                if mode == "fifo":
                    os.mkfifo(path, 0o600)
                elif mode != "missing":
                    path.write_text(original)
                    path.chmod(0o600)
                    if mode == "unbound":
                        self.edit_record(native_id=None)
                    else:
                        path.write_text("bad json")
                with self.assertRaises(ConfigError):
                    api.start("codex", "headless", "again", self.spec, resume=self.spec.sid)
                self.assertEqual(len(self.logged()), 1)

    def test_missing_session_does_not_spawn(self):
        self.drain(api.start("codex", "headless", "one", self.spec))
        next((self.codex_home / "sessions").rglob("*.jsonl")).unlink()
        with self.assertRaisesRegex(ConfigError, "missing.*file"):
            api.start("codex", "headless", "again", self.spec, resume=self.spec.sid)
        self.assertEqual(len(self.logged()), 1)

    def test_usage_limit_passes_through_driver(self):
        self.config["adapters"]["codex"]["env"]["FAKE_LIMIT"] = "1"
        found = self.drain(api.start("codex", "headless", "one", self.spec))
        self.assertIn(LimitHit(None), found)
        self.assertEqual(found[-1], Exited(0))

    def test_caller_sid_and_handle_info_inspect_native_binding(self):
        spec = dataclasses.replace(self.spec, sid=None)
        handle = api.start("codex", "headless", "hello", spec, sid="caller-owned")
        self.drain(handle)
        info = handle.info()
        self.assertEqual((handle.sid, info.sid, info.native_id, info.resumable),
                         ("caller-owned", "caller-owned", NATIVE, "yes"))
        self.assertTrue(info.transcript.endswith(f"-{NATIVE}.jsonl"))
        self.assertIsNotNone(info.last_active)
        self.assertIn(NATIVE, info.resume_command)
