import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_session  # noqa: E402
from clients import ClaudeClient  # noqa: E402
from clients.base import Client, SessionInfo  # noqa: E402
from compose import ConfigError  # noqa: E402


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.work = os.path.join(self.tmp.name, "work")
        self.cwd = os.path.join(self.work, "project")
        self.extra = os.path.join(self.tmp.name, "extra")
        self.codex_home = os.path.join(self.home, ".codex")
        for path in (self.home, self.cwd, self.extra, self.codex_home):
            os.makedirs(path)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": self.home}))
        self.sid = "../odd/opaque ID with spaces"
        self.native = "11111111-2222-3333-4444-555555555555"
        self.snapshot = dict(cwd=self.cwd, project=False, dirs=[self.extra], commands=["git status"],
                             model="gpt-test", effort="low", network=False, codex_home=self.codex_home)

    def create(self, **kw):
        return run_session.create(self.work, self.sid, "codex", kw.get("snapshot", self.snapshot),
                                  kw.get("writable_roots", [self.work, self.cwd, self.extra, self.codex_home]))

    def test_opaque_sid_and_durable_round_trip(self):
        with mock.patch.object(run_session.os, "fsync", wraps=os.fsync) as sync:
            state = self.create()
            self.assertGreaterEqual(sync.call_count, 2)
        self.assertEqual(state.sid, self.sid)
        self.assertIsNone(state.client_id)
        self.assertEqual(run_session.load(self.work, self.sid), state)
        store = os.path.join(self.home, ".agent-pm", "core-sessions")
        files = os.listdir(store)
        self.assertEqual(len(files), 1)
        self.assertNotIn("opaque", files[0])
        self.assertEqual(stat.S_IMODE(os.stat(store).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(store, files[0])).st_mode), 0o600)
        with mock.patch.object(run_session.os, "fsync", wraps=os.fsync) as sync:
            bound = run_session.bind(self.work, self.sid, self.native)
            self.assertGreaterEqual(sync.call_count, 2)
        self.assertEqual(bound.client_id, self.native)
        self.assertEqual(run_session.bind(self.work, self.sid, self.native), bound)
        with self.assertRaisesRegex(ConfigError, "different|changed|bound"):
            run_session.bind(self.work, self.sid, "aaaaaaaa-2222-3333-4444-555555555555")

    def test_duplicate_and_missing(self):
        self.assertIsNone(run_session.load(self.work, self.sid))
        with self.assertRaisesRegex(ConfigError, "missing|no.*state"):
            run_session.bind(self.work, self.sid, self.native)
        self.create()
        with self.assertRaisesRegex(ConfigError, "already|duplicate"):
            self.create()

    def test_rejects_writable_overlap(self):
        root = os.path.join(self.home, ".agent-pm", "core-sessions")
        for writable in (self.home, root, os.path.dirname(root)):
            with self.subTest(writable=writable), self.assertRaisesRegex(ConfigError, "writable"):
                self.create(writable_roots=[writable])
        changed = dict(self.snapshot, cwd=self.home)
        with self.assertRaisesRegex(ConfigError, "writable"):
            self.create(snapshot=changed, writable_roots=[])

    def test_existing_owner_controlled_config_parent_can_be_0755(self):
        parent = os.path.join(self.home, ".agent-pm")
        os.mkdir(parent, 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(parent).st_mode), 0o755)
        self.create()
        store = os.path.join(parent, "core-sessions")
        self.assertEqual(stat.S_IMODE(os.stat(store).st_mode), 0o700)
        self.assertIsNotNone(run_session.load(self.work, self.sid))
        os.chmod(parent, 0o775)
        with self.assertRaisesRegex(ConfigError, "mode|permission"):
            run_session.load(self.work, self.sid)
        changed = dict(self.snapshot, codex_home=self.home)
        with self.assertRaisesRegex(ConfigError, "writable"):
            self.create(snapshot=changed, writable_roots=[])

    def test_rejects_symlink_and_unsafe_permissions(self):
        parent = os.path.join(self.home, ".agent-pm")
        target = os.path.join(self.tmp.name, "target")
        os.mkdir(target)
        os.symlink(target, parent)
        with self.assertRaisesRegex(ConfigError, "symlink"):
            self.create()
        os.unlink(parent)
        self.create()
        store = os.path.join(parent, "core-sessions")
        os.chmod(store, 0o777)
        with self.assertRaisesRegex(ConfigError, "mode|permission"):
            run_session.load(self.work, self.sid)
        os.chmod(store, 0o700)
        path = os.path.join(store, os.listdir(store)[0])
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(ConfigError, "mode|permission"):
            run_session.load(self.work, self.sid)
        os.chmod(path, 0o600)
        with mock.patch.object(run_session.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ConfigError, "owner"):
                run_session.load(self.work, self.sid)
        os.unlink(path)
        os.symlink(target, path)
        with self.assertRaisesRegex(ConfigError, "symlink"):
            run_session.load(self.work, self.sid)

    def test_rejects_malformed_state_and_snapshot(self):
        self.create()
        store = os.path.join(self.home, ".agent-pm", "core-sessions")
        path = os.path.join(store, os.listdir(store)[0])
        for bad in ({"version": 1}, {"version": 1, "workdir": self.work, "sid": self.sid,
                                            "client": "codex", "snapshot": dict(self.snapshot, network="yes"),
                                            "client_id": None}):
            with self.subTest(bad=bad), open(path, "w") as handle:
                json.dump(bad, handle)
            with self.assertRaisesRegex(ConfigError, "invalid|malformed"):
                run_session.load(self.work, self.sid)

    def test_rejects_retargeted_snapshot_path_and_forged_workdir_store(self):
        self.create()
        store = os.path.join(self.home, ".agent-pm", "core-sessions")
        path = os.path.join(store, os.listdir(store)[0])
        with open(path) as handle:
            data = json.load(handle)
        data["snapshot"]["cwd"] = os.path.join(self.work, "..", "work", "project")
        with open(path, "w") as handle:
            json.dump(data, handle)
        with self.assertRaisesRegex(ConfigError, "snapshot path"):
            run_session.load(self.work, self.sid)
        forged_home = os.path.join(self.work, "fake-home")
        os.makedirs(os.path.join(forged_home, ".agent-pm", "core-sessions"), mode=0o700)
        os.chmod(os.path.join(forged_home, ".agent-pm"), 0o700)
        with mock.patch.dict(os.environ, {"HOME": forged_home}):
            with self.assertRaisesRegex(ConfigError, "writable"):
                run_session.create(self.work, "forged", "codex", self.snapshot, [])


class Capabilities(unittest.TestCase):
    def test_default_and_claude_resume(self):
        unsupported = Client({}).session_info("/work", "sid")
        self.assertEqual(unsupported, SessionInfo())
        self.assertFalse(unsupported.resume_supported)
        self.assertFalse(unsupported.inspection_supported)
        self.assertIsNone(Client({}).resume_location("/work", "sid"))
        claude = ClaudeClient({"tiers": {}, "efforts": {}}).session_info("/work", "sid")
        self.assertTrue(claude.resume_supported)
        self.assertFalse(claude.inspection_supported)
        self.assertFalse(claude.usage_supported)


if __name__ == "__main__":
    unittest.main()
