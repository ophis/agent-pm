import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import hermetic  # noqa: F401
from clients import codex
from clients.claude import ClaudeClient
from clients.base import Client, SessionInfo
from compose import ConfigError


class Store(unittest.TestCase):
    def setUp(self):
        self.implicit = self.enterContext(mock.patch.object(codex, "_implicit_writable_roots", return_value=[]))
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
        return codex._create_session(self.work, self.sid, kw.get("snapshot", self.snapshot),
                                  kw.get("writable_roots", [self.work, self.cwd, self.extra, self.codex_home]))

    def test_opaque_sid_and_durable_round_trip(self):
        with mock.patch.object(codex.os, "fsync", wraps=os.fsync) as sync:
            state = self.create()
            self.assertGreaterEqual(sync.call_count, 2)
        self.assertEqual(state.sid, self.sid)
        self.assertIsNone(state.client_id)
        self.assertEqual(codex._load_session(self.work, self.sid), state)
        store = os.path.join(self.home, ".agent-pm", "core-sessions")
        files = os.listdir(store)
        self.assertEqual(len(files), 1)
        self.assertNotIn("opaque", files[0])
        self.assertEqual(stat.S_IMODE(os.stat(store).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(store, files[0])).st_mode), 0o600)
        with mock.patch.object(codex.os, "fsync", wraps=os.fsync) as sync:
            bound = codex._bind_session(self.work, self.sid, self.native)
            self.assertGreaterEqual(sync.call_count, 2)
        self.assertEqual(bound.client_id, self.native)
        self.assertEqual(codex._bind_session(self.work, self.sid, self.native), bound)
        with self.assertRaisesRegex(ConfigError, "different|changed|bound"):
            codex._bind_session(self.work, self.sid, "aaaaaaaa-2222-3333-4444-555555555555")

    def test_stored_snapshot_and_native_id_round_trip(self):
        payload = {"conversation": {"cursor": [7, None, True]}, "vendor": "codex"}
        state = codex._create_session(self.work, self.sid, payload, [self.extra])
        self.assertEqual(codex._load_session(self.work, self.sid), state)
        self.assertEqual(state.snapshot, payload)
        self.assertEqual(state.writable_roots, [os.path.realpath(self.work), os.path.realpath(self.extra)])
        self.assertEqual(codex._bind_session(self.work, self.sid, "native:opaque-id").client_id, "native:opaque-id")

    def test_rejects_non_json_payload(self):
        for payload in ({"x": float("nan")}, {"x": {1, 2}}, {1: "integer key"}, {"x": (1, 2)}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ConfigError, "snapshot"):
                self.create(snapshot=payload)

    def test_load_rechecks_saved_writable_roots(self):
        self.create()
        root = os.path.join(self.home, ".agent-pm", "core-sessions")
        path = os.path.join(root, os.listdir(root)[0])
        with open(path) as f:
            data = json.load(f)
        data["writable_roots"].append(os.path.realpath(self.home))
        with open(path, "w") as f:
            json.dump(data, f)
        with self.assertRaisesRegex(ConfigError, "writable"):
            codex._load_session(self.work, self.sid)

    def test_private_fifo_is_rejected_without_blocking(self):
        self.create()
        root = os.path.join(os.path.realpath(self.home), ".agent-pm", "core-sessions")
        path = codex._path(root, os.path.realpath(self.work), self.sid)
        os.unlink(path)
        os.mkfifo(path, 0o600)
        code = """import sys
from clients import codex
from compose import ConfigError
try:
    codex._read(*sys.argv[1:])
except ConfigError as exc:
    print(exc)
else:
    sys.exit(1)
"""
        result = subprocess.run([sys.executable, "-c", code, path, self.work, self.sid],
                                cwd=os.path.dirname(codex.__file__) + "/..", capture_output=True,
                                text=True, timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unsafe recovery state file", result.stdout)

    def test_deep_private_json_is_a_config_error(self):
        self.create()
        root = os.path.join(os.path.realpath(self.home), ".agent-pm", "core-sessions")
        path = codex._path(root, os.path.realpath(self.work), self.sid)
        with open(path, "w") as stream:
            stream.write("[" * 2000 + "0" + "]" * 2000)
        decoder = json.JSONDecoder()
        decoder.scan_once = json.scanner.py_make_scanner(decoder)
        with mock.patch.object(json, "_default_decoder", decoder), \
                self.assertRaisesRegex(ConfigError, "malformed recovery state"):
            codex._load_session(self.work, self.sid)

    def test_duplicate_and_missing(self):
        self.assertIsNone(codex._load_session(self.work, self.sid))
        with self.assertRaisesRegex(ConfigError, "missing|no.*state"):
            codex._bind_session(self.work, self.sid, self.native)
        self.create()
        with self.assertRaisesRegex(ConfigError, "already|duplicate"):
            self.create()

    def test_rejects_writable_overlap(self):
        root = os.path.join(self.home, ".agent-pm", "core-sessions")
        for writable in (self.home, root, os.path.dirname(root)):
            with self.subTest(writable=writable), self.assertRaisesRegex(ConfigError, "writable"):
                self.create(writable_roots=[writable])

    def test_existing_owner_controlled_config_parent_can_be_0755(self):
        parent = os.path.join(self.home, ".agent-pm")
        os.mkdir(parent, 0o755)
        self.assertEqual(stat.S_IMODE(os.stat(parent).st_mode), 0o755)
        self.create()
        store = os.path.join(parent, "core-sessions")
        self.assertEqual(stat.S_IMODE(os.stat(store).st_mode), 0o700)
        self.assertIsNotNone(codex._load_session(self.work, self.sid))
        os.chmod(parent, 0o775)
        with self.assertRaisesRegex(ConfigError, "mode|permission"):
            codex._load_session(self.work, self.sid)

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
            codex._load_session(self.work, self.sid)
        os.chmod(store, 0o700)
        path = os.path.join(store, os.listdir(store)[0])
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(ConfigError, "mode|permission"):
            codex._load_session(self.work, self.sid)
        os.chmod(path, 0o600)
        with mock.patch.object(codex.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ConfigError, "owner"):
                codex._load_session(self.work, self.sid)
        os.unlink(path)
        os.symlink(target, path)
        with self.assertRaisesRegex(ConfigError, "symlink"):
            codex._load_session(self.work, self.sid)

    def test_rejects_malformed_state_and_snapshot(self):
        self.create()
        store = os.path.join(self.home, ".agent-pm", "core-sessions")
        path = os.path.join(store, os.listdir(store)[0])
        for bad in ({"version": codex._SESSION_VERSION}, {"version": codex._SESSION_VERSION, "workdir": self.work, "sid": self.sid,
                                            "client": "codex", "snapshot": dict(self.snapshot, network="yes"),
                                            "client_id": None, "writable_roots": "bad"}):
            with self.subTest(bad=bad), open(path, "w") as handle:
                json.dump(bad, handle)
            with self.assertRaisesRegex(ConfigError, "invalid|malformed"):
                codex._load_session(self.work, self.sid)

    def test_rejects_retargeted_snapshot_path_and_forged_workdir_store(self):
        self.create()
        store = os.path.join(self.home, ".agent-pm", "core-sessions")
        path = os.path.join(store, os.listdir(store)[0])
        with open(path) as handle:
            data = json.load(handle)
        data["writable_roots"][0] = os.path.join(self.work, "..", "work")
        with open(path, "w") as handle:
            json.dump(data, handle)
        with self.assertRaisesRegex(ConfigError, "writable root"):
            codex._load_session(self.work, self.sid)
        forged_home = os.path.join(self.work, "fake-home")
        os.makedirs(os.path.join(forged_home, ".agent-pm", "core-sessions"), mode=0o700)
        os.chmod(os.path.join(forged_home, ".agent-pm"), 0o700)
        with mock.patch.dict(os.environ, {"HOME": forged_home}):
            with self.assertRaisesRegex(ConfigError, "writable"):
                codex._create_session(self.work, "forged", self.snapshot, [])


class ImplicitWritableRoots(unittest.TestCase):
    def test_temporary_home_rejected_on_create_and_load_without_saved_roots(self):
        # No helper patch: loading must reject the location before trusting any file content.
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            work = os.path.realpath("/isolated-work")
            root = os.path.join(home, ".agent-pm", "core-sessions")
            os.makedirs(root, mode=0o700)
            os.chmod(os.path.dirname(root), 0o700)
            path = codex._path(root, work, "sid")
            with open(path, "w") as f:
                json.dump({"version": codex._SESSION_VERSION, "workdir": work, "sid": "sid", "client": "codex",
                           "snapshot": {}, "client_id": None, "writable_roots": []}, f)
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigError, "writable"):
                codex._create_session(work, "new", {}, [])
            with self.assertRaisesRegex(ConfigError, "writable"):
                codex._load_session(work, "sid")

    def test_implicit_locations_include_tmpdir_and_system_temp_roots(self):
        with mock.patch.dict(os.environ, {"TMPDIR": "/custom/agent-temp"}):
            roots = set(map(os.path.realpath, codex._implicit_writable_roots()))
        self.assertLessEqual(set(map(os.path.realpath, ["/custom/agent-temp", "/tmp", "/private/tmp", "/var/tmp"])), roots)


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
