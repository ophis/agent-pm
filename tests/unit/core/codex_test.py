import contextlib
import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest import mock

from tests import ROOT, hermetic
from tests.integration.core import fake_codex
from clients import _codex_worker, codex
from clients.claude import ClaudeClient
from clients.base import Client, SessionInfo, WorkerSpec
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


SID = "0b6f1c2e-3d4a-4b5c-8d6e-7f8091a2b3c4"
NATIVE = "11111111-2222-4333-8444-555555555555"
HOOK = os.path.join(ROOT, "core", "src", "clients", "_codex_hook.py")
NOTIFY = 'tui.notification_method="bel"'
WORDS = (("SessionStart", "session"), ("UserPromptSubmit", "working"), ("PermissionRequest", "blocked"),
         ("Stop", "done"), ("Interrupt", "interrupt"))
RESERVED = ": crew.py sets a Codex worker's hooks, session and cwd"
UNBOUND = f"Codex worker {SID} has no native session: it never took a prompt; start a new worker"
# The worker's app-server: the repo's fake codex (fake_codex.py, its scenario in $FAKE_CODEX_SCENARIO), its catalog
# broken as $FAKE_APP says: die (the first n runs exit 1 at once), drop (eventNames), change (eventName → fields of
# our entry), extra (entries), error (every request after initialize answered with it), deaf (a child in its process
# group; hooks/list never answered, stdin's end ignored), cwds (hooks/list's data: one entry per cwd given, whatever
# was asked). Runs and pids are appended to files beside $FAKE_APP.
APP_SERVER = """import json, os, subprocess, sys, time
fakes = sys.argv.pop(1)
app_file = os.environ["FAKE_APP"]
with open(app_file) as f:
    app = json.load(f)
with open(app_file + ".runs", "a") as f:
    f.write(f"{os.getpid()}\\n")
with open(app_file + ".runs") as f:
    if len(f.readlines()) <= app.get("die", 0):
        sys.exit(1)
if app.get("deaf"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(app_file + ".pids", "w") as f:
        json.dump([os.getpid(), child.pid], f)
if app.get("deaf") or "error" in app:
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        if msg["method"] == "initialize":
            print(json.dumps({"id": msg["id"], "result": {}}), flush=True)
        elif "error" in app:
            print(json.dumps({"id": msg["id"], "error": {"code": -32603, "message": app["error"]}}), flush=True)
        else:
            time.sleep(60)
    sys.exit(0)
sys.path.insert(0, fakes)
import fake_codex
if "cwds" in app:
    config = fake_codex.configure(fake_codex.parser("codex app-server").parse_args(sys.argv[2:]).overrides)
    hooks = fake_codex.catalog(config, fake_codex.codex_home(), None)
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" in msg:
            result = {"data": [{"cwd": c, "hooks": hooks, "warnings": [], "errors": []} for c in app["cwds"]]}
            print(json.dumps({"id": msg["id"], "result": {} if msg["method"] == "initialize" else result}), flush=True)
    sys.exit(0)
catalog = fake_codex.catalog


def broken(config, home, user_hook):
    hooks = [h for h in catalog(config, home, user_hook) if h["eventName"] not in app.get("drop", ())]
    for h in hooks:
        if h["source"] == "sessionFlags":
            h.update(app.get("change", {}).get(h["eventName"], {}))
    return hooks + app.get("extra", []) if hooks else hooks


fake_codex.catalog = broken
sys.exit(fake_codex.app_server(sys.argv[1:]))
"""


def hook_args(events: str, record: str, tmux: str) -> list[str]:
    """A Codex worker's hook -c args (core/src/clients/_codex_worker.py): resolved paths."""
    args = []
    python, hook, tmux = map(os.path.realpath, (sys.executable, HOOK, tmux))
    for event, word in WORDS:
        command = json.dumps(shlex.join([python, "-I", hook, word, events, record, tmux]), ensure_ascii=False)
        args += ["-c", f'hooks.{event}=[{{hooks=[{{type="command",command={command},'
                       f'timeout={3 if event == "Interrupt" else 10}}}]}}]']
    return args


@contextlib.contextmanager
def virtual_time():
    """time.monotonic as a clock that time.sleep advances, and select.select too, by its whole timeout, when nothing
    is ready within 0.5 s."""
    now, real_select = [time.monotonic()], select.select

    def wait(rlist, wlist, xlist, timeout):
        ready = real_select(rlist, wlist, xlist, 0.5)
        if not any(ready):
            now[0] += timeout
        return ready

    def sleep(seconds):
        now[0] += seconds
    with mock.patch.object(time, "monotonic", lambda: now[0]), mock.patch.object(select, "select", wait), \
            mock.patch.object(time, "sleep", sleep):
        yield


class Worker(unittest.TestCase):
    """CodexClient.worker and worker_reply (clients/_codex_worker.py) with a fake app-server (APP_SERVER), in a HOME
    of the test's own."""

    def setUp(self):
        self.home = os.path.realpath(os.path.dirname(hermetic.home(self)))
        self.workers = os.path.join(self.home, ".agent-pm", "core-sessions", "workers")
        self.record = os.path.join(self.workers, f"{SID}.json")
        self.codex_home = os.path.join(self.home, ".codex")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.bin = os.path.join(self.dir, "bin")
        self.cwd = os.path.join(self.dir, "project")
        for path in (self.bin, self.cwd):
            os.mkdir(path)
        wrapper = os.path.join(self.dir, "app_server.py")
        with open(wrapper, "w") as f:
            f.write(APP_SERVER)
        self.codex = os.path.join(self.bin, "codex")
        fakes = os.path.dirname(fake_codex.__file__)
        self.executable(self.codex, f'exec {shlex.join([sys.executable, wrapper, fakes])} "$@"')
        self.tmux = os.path.join(self.bin, "tmux")
        self.executable(self.tmux, "exit 1")
        self.events = os.path.join(self.dir, "events")
        os.close(os.open(self.events, os.O_WRONLY | os.O_CREAT, 0o600))
        self.log = os.path.join(self.dir, "log")
        self.scenario_file = os.path.join(self.dir, "scenario.json")
        self.app = os.path.join(self.dir, "app.json")
        self.scenario()
        self.breaks()
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", fake_codex.ENV: self.scenario_file, "FAKE_APP": self.app}
        self.client = codex.CodexClient({"tiers": {}, "efforts": {}, "shell_network": False})

    def executable(self, path, body):
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\n{body}\n")
        os.chmod(path, 0o755)

    def scenario(self, **keys):
        with open(self.scenario_file, "w") as f:
            json.dump({"log": self.log, **keys}, f)

    def breaks(self, **app):
        with open(self.app, "w") as f:
            json.dump(app, f)

    def spec(self, **kw):
        return WorkerSpec(**{"name": "w1", "sid": SID, "cwd": self.cwd, "flags": (), "prompt": None,
                             "events": self.events, "resume": False, "env": self.env, **kw})

    def logged(self, key):
        try:
            with open(self.log) as f:
                return [line[key] for line in map(json.loads, f) if key in line]
        except FileNotFoundError:
            return []

    def runs(self):
        try:
            with open(self.app + ".runs") as f:
                return len(f.readlines())
        except FileNotFoundError:
            return 0

    def make_record(self, native=NATIVE, **fields):
        """The worker record, its dirs made as worker() makes them."""
        path = os.path.dirname(self.workers)
        for d in (path, self.workers):
            if not os.path.isdir(d):
                os.mkdir(d, 0o700)
        fd = os.open(self.record, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "sid": SID, "cwd": self.cwd, "codex_home": self.codex_home, "native": native,
                       **fields}, f)

    def hooks(self):
        return hook_args(self.events, self.record, self.tmux)

    def state(self, hooks):
        """The hooks.state -c arg trusting hooks as the fake's catalog lists them."""
        entries = fake_codex.catalog(fake_codex.configure([("config", a) for a in hooks[1::2]]), self.codex_home, None)
        return "hooks.state={" + ",".join(f'{json.dumps(e["key"])}={{trusted_hash={json.dumps(e["currentHash"])}}}'
                                          for e in entries) + "}"

    def fails(self, **kw):
        with self.assertRaises(ConfigError) as cm:
            self.client.worker(self.spec(**kw))
        return str(cm.exception)

    def test_a_new_worker(self):
        """BEL notifications first, the hooks, their trust, the user's flags, `--` and the prompt; a new record; the
        app-server given the hooks and the user's -c, --enable and --disable items, asked for the worker's cwd."""
        flags = ("-m", "gpt-x", "-c", 'tui.notification_method="auto"', "--config=model_verbosity=\"low\"",
                 "--enable", "apps", "--disable=memory")
        with mock.patch.object(os, "fsync", wraps=os.fsync) as sync:
            cmd = self.client.worker(self.spec(flags=flags, prompt="-do it"))
        self.assertGreaterEqual(sync.call_count, 2)
        hooks = self.hooks()
        self.assertEqual(cmd.argv, [self.codex, "--no-daemon", "-C", self.cwd, "-c", NOTIFY, *hooks, "-c",
                                    self.state(hooks), *flags, "--", "-do it"])
        self.assertEqual(cmd.env, {"CODEX_HOME": self.codex_home})
        with open(self.record) as f:
            self.assertEqual(json.load(f), {"version": 1, "sid": SID, "cwd": self.cwd, "codex_home": self.codex_home,
                                            "native": None})
        self.assertEqual(stat.S_IMODE(os.stat(self.record).st_mode), 0o600)
        for d in (self.workers, os.path.dirname(self.workers)):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        [start] = self.logged("argv")
        self.assertEqual(start, ["app-server", *hooks, "-c", 'tui.notification_method="auto"', "-c",
                                 'model_verbosity="low"', "--enable", "apps", "--disable", "memory"])
        self.assertEqual(self.logged("cwd"), [self.cwd])
        self.assertEqual(self.logged("env")[0]["CODEX_HOME"], self.codex_home)
        self.assertEqual(self.logged("received"), [
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agent-pm", "version": "0"},
                                                         "capabilities": {"experimentalApi": True}}},
            {"method": "initialized"}, {"id": 2, "method": "hooks/list", "params": {"cwds": [self.cwd]}}])

    def test_hooks_run_the_resolved_paths_protect_checks(self):
        links = os.path.join(self.dir, "links")
        os.mkdir(links)
        python, hook, tmux = (os.path.join(links, n) for n in ("python", "hook.py", "tmux"))
        for link, target in ((python, sys.executable), (hook, HOOK), (tmux, self.tmux)):
            os.symlink(target, link)
        hooks = self.hooks()
        with mock.patch.object(sys, "executable", python), mock.patch.object(_codex_worker, "HOOK", hook):
            cmd = self.client.worker(self.spec(env={**self.env, "PATH": f"{links}:{self.env['PATH']}"}))
        self.assertEqual(cmd.argv, [self.codex, "--no-daemon", "-C", self.cwd, "-c", NOTIFY, *hooks, "-c",
                                    self.state(hooks)])

    def test_codex_home_and_the_binaries_from_the_env(self):
        home = os.path.join(self.dir, "codex-home")
        cmd = self.client.worker(self.spec(env={**self.env, "CODEX_HOME": home}))
        self.assertEqual(cmd.env, {"CODEX_HOME": home})
        self.assertEqual(self.logged("env")[0]["CODEX_HOME"], home)
        for name in ("codex", "tmux"):
            with self.subTest(name=name):
                bare = os.path.join(self.dir, f"no-{name}")
                os.mkdir(bare)
                for other in ("codex", "tmux"):
                    if other != name:
                        os.symlink(os.path.join(self.bin, other), os.path.join(bare, other))
                self.assertEqual(self.fails(sid=SID.replace("0b", "0c"), env={**self.env, "PATH": bare}),
                                 f"{name} not found on PATH")

    def test_a_resumed_worker(self):
        """resume <native> -C <cwd> first; the record's CODEX_HOME whatever the env's; the stored executable."""
        self.make_record()
        stored = os.path.join(self.dir, "stored", "codex")
        os.mkdir(os.path.dirname(stored))
        os.symlink(self.codex, stored)
        for prompt, tail in ((None, []), ("next", ["--", "next"])):
            with self.subTest(prompt=prompt):
                cmd = self.client.worker(self.spec(resume=True, flags=("-m", "x"), prompt=prompt, exe=stored,
                                                   env={**self.env, "CODEX_HOME": "/elsewhere"}))
                hooks = self.hooks()
                self.assertEqual(cmd.argv, [stored, "resume", NATIVE, "-C", self.cwd, "--no-daemon", "-c", NOTIFY,
                                            *hooks, "-c", self.state(hooks), "-m", "x", *tail])
                self.assertEqual(cmd.env, {"CODEX_HOME": self.codex_home})
                self.assertEqual(self.logged("env")[-1]["CODEX_HOME"], self.codex_home)

    def test_reserved_flags_refused_before_a_bare_double_dash(self):
        for flags in (("-c", "hooks.Stop=[]"), ("-chooks.Stop=[]",), ("-c=hooks.x=1",), ("--config", "hooks={}"),
                      ("--config=hooks.state={}",), ("-c", ' "hooks" .Stop=[]'), ("-c", "hooks"),
                      ("-c", "sandbox_workspace_write.network_access=true"), ("--dangerously-bypass-hook-trust",),
                      ("--dangerously-bypass-hook-trust=true",), ("-C", "/x"), ("-C/x",), ("--cd", "/x"),
                      ("--cd=/x",), ("-p", "work"), ("-pwork",), ("--profile", "work"), ("--profile=work",),
                      ("resume",), ("fork",), ("--last",)):
            with self.subTest(flags=flags):
                self.assertEqual(self.fails(flags=("-m", "m", *flags, "--", "resume")), " ".join(flags) + RESERVED)
        self.assertFalse(os.path.exists(self.workers))
        self.assertEqual(self.runs(), 0)
        cmd = self.client.worker(self.spec(flags=("-c", "features.hooks=true", "--", "resume", "-C", "/x")))
        self.assertEqual(cmd.argv[-4:], ["--", "resume", "-C", "/x"])

    def test_protected_paths_refused(self):
        """A writable root (the cwd, each --add-dir) holding or inside core's src/, the record dir, the tmux binary, the
        codex executable (resolved) or the events file."""
        core = os.path.realpath(os.path.join(ROOT, "core"))
        m, cx, links = (os.path.join(self.dir, n) for n in ("m", "cx", "links"))
        for d in (m, cx, links):
            os.mkdir(d)
        self.executable(os.path.join(cx, "codex"), "exit 1")
        os.symlink(os.path.join(cx, "codex"), os.path.join(links, "codex"))
        events = os.path.join(m, "events")
        os.close(os.open(events, os.O_WRONLY | os.O_CREAT, 0o600))
        inside = os.path.join(self.workers, "w")
        for kw, root, path in (({"cwd": core}, core, os.path.join(core, "src")),
                               ({"cwd": inside}, inside, self.workers),
                               ({"cwd": self.home}, self.home, self.workers),
                               ({"cwd": self.bin}, self.bin, self.tmux),
                               ({"cwd": cx, "env": {**self.env, "PATH": f"{links}:{self.env['PATH']}"}}, cx,
                                os.path.join(cx, "codex")),
                               ({"flags": ("--add-dir", m), "events": events}, m, events),
                               ({"flags": ("--add-dir=" + m,), "events": events}, m, events),
                               ({"flags": ("--add-dir", "../m"), "events": events}, m, events)):
            with self.subTest(**kw):
                self.assertEqual(self.fails(**kw), f"Codex worker {root} could rewrite {path}, which its hooks run "
                                                   "unsandboxed: use another --cwd")
        self.assertFalse(os.path.exists(self.workers))
        self.assertEqual(self.runs(), 0)

    def test_a_record_is_made_once(self):
        self.client.worker(self.spec())
        with open(self.record, "rb") as f:
            before = f.read()
        self.assertEqual(self.fails(), f"Codex worker record {self.record} already exists")
        with open(self.record, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.fails(sid="../x"), "bad session id '../x'")

    def test_resume_needs_its_bound_record_in_its_cwd(self):
        missing = f"no Codex worker record for {SID}"
        self.assertEqual(self.fails(resume=True), missing)
        self.make_record(None)
        self.assertEqual(self.fails(resume=True), UNBOUND)
        self.make_record()
        other = os.path.join(self.dir, "other")
        os.mkdir(other)
        self.assertEqual(self.fails(resume=True, cwd=other), f"--cwd {other}: Codex worker {SID} ran in {self.cwd}")
        os.unlink(self.record)
        self.assertEqual(self.fails(resume=True), missing)
        self.assertEqual(self.runs(), 0)

    def test_a_bad_record_refused(self):
        target = os.path.join(self.dir, "target.json")
        malformed = f"malformed Codex worker record {self.record}"
        for name, setup, want in (
                ("symlink", lambda: os.symlink(target, self.record), "symbolic links"),
                ("mode", lambda: os.chmod(self.record, 0o644), "not a 0600 regular file of yours"),
                ("extra key", lambda: self.make_record(extra=1), malformed),
                ("other sid", lambda: self.make_record(sid=SID.replace("0b", "0c")), malformed),
                ("version", lambda: self.make_record(version=True), malformed),
                ("native", lambda: self.make_record(NATIVE.replace("1", "A")), malformed),
                ("relative cwd", lambda: self.make_record(cwd="project"), malformed),
                ("not JSON", lambda: open(self.record, "w").close(), malformed)):
            with self.subTest(name):
                self.make_record()
                if name == "symlink":
                    os.rename(self.record, target)
                setup()
                for call in (lambda: self.client.worker(self.spec(resume=True)),
                             lambda: self.client.worker_reply(SID)):
                    with self.assertRaises(ConfigError) as cm:
                        call()
                    if want == malformed:
                        self.assertEqual(str(cm.exception), malformed)
                    else:
                        self.assertRegex(str(cm.exception), f"^{re.escape(f'Codex worker record {self.record}: ')}.*"
                                                            + re.escape(want))
                os.unlink(self.record)
        self.make_record()
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1), self.assertRaises(ConfigError):
            self.client.worker_reply(SID)
        os.rename(self.workers, os.path.join(self.dir, "workers"))
        os.symlink(os.path.join(self.dir, "workers"), self.workers)
        with self.assertRaisesRegex(ConfigError, "symlink"):
            self.client.worker_reply(SID)
        self.assertEqual(self.runs(), 0)

    def test_hooks_off_fails_closed_leaving_the_record(self):
        for flags in (("--disable", "hooks"), ("-c", "features.hooks=false")):
            with self.subTest(flags=flags):
                if os.path.exists(self.record):
                    os.unlink(self.record)
                self.assertEqual(self.fails(flags=flags), "codex hooks/list: Codex hooks are off (features.hooks); a "
                                                          "Codex worker needs them")
                with open(self.record) as f:
                    self.assertIsNone(json.load(f)["native"])

    def test_only_our_five_hooks_as_defined_are_trusted(self):
        extra = {"key": "/<session-flags>/config.toml:subagent_stop:0:0", "eventName": "subagentStop",
                 "handlerType": "command", "command": "true", "source": "sessionFlags", "enabled": True,
                 "isManaged": False, "currentHash": "sha256:" + "0" * 64, "trustStatus": "untrusted"}
        for app, why in (({"drop": ["stop"]}, "no session-flags stop hook"),
                         ({"extra": [{**extra, "eventName": "stop"}]}, "2 session-flags stop hooks"),
                         ({"change": {"stop": {"enabled": False}}}, "the session-flags stop hook is disabled"),
                         ({"change": {"stop": {"command": "true"}}}, "the session-flags stop hook is not our command"),
                         ({"change": {"stop": {"handlerType": "prompt"}}},
                          "the session-flags stop hook is not our command"),
                         ({"change": {"stop": {"key": 7}}}, "the session-flags stop hook has no key"),
                         ({"change": {"stop": {"currentHash": "sha256:abc"}}},
                          "the session-flags stop hook has no sha256 hash"),
                         ({"extra": [extra]}, f"a session-flags hook not ours: {extra['key']}"),
                         ({"error": "boom"}, "hooks/list failed: boom")):
            with self.subTest(app=app):
                self.breaks(**app)
                if os.path.exists(self.record):
                    os.unlink(self.record)
                self.assertEqual(self.fails(), f"codex hooks/list: {why}")
        self.breaks()
        os.unlink(self.record)
        self.scenario(errors=["bad project config"])
        self.assertEqual(self.fails(), f'codex hooks/list: errors for {self.cwd}: ["bad project config"]')

    def test_the_one_answer_is_used_whatever_its_cwd_says(self):
        """One cwd asked: one entry wanted, its cwd as Codex writes it (here the other form of /private/...)."""
        other = self.cwd.removeprefix("/private") if self.cwd.startswith("/private/") else "/private" + self.cwd
        self.breaks(cwds=[other])
        hooks = self.hooks()
        self.assertEqual(self.client.worker(self.spec()).argv[-1], self.state(hooks))
        for cwds in ([], [self.cwd, self.cwd], [self.cwd, other]):
            with self.subTest(cwds=cwds):
                self.breaks(cwds=cwds)
                os.unlink(self.record)
                self.assertEqual(self.fails(), f"codex hooks/list: want one answer for {self.cwd}, got {len(cwds)}")

    def test_the_users_own_untrusted_or_modified_hook_fails_closed(self):
        key = f"{self.codex_home}/hooks.json:stop:0:0"
        for hook, refused in (({"trustStatus": "untrusted"}, "untrusted"), ({"trustStatus": "modified"}, "modified"),
                              ({"trustStatus": "trusted"}, None),
                              ({"trustStatus": "untrusted", "enabled": False}, None),
                              ({"trustStatus": "modified", "isManaged": True}, None)):
            with self.subTest(hook=hook):
                self.scenario(user_hook=hook)
                if os.path.exists(self.record):
                    os.unlink(self.record)
                if refused is None:
                    hooks = self.hooks()
                    self.assertEqual(self.client.worker(self.spec()).argv[-1], self.state(hooks))
                else:
                    self.assertEqual(self.fails(), f"codex hooks/list: your hook {key} is {refused}: review it in "
                                                   "your own Codex session (/hooks) first")

    def test_one_retry_when_the_app_server_exits_before_answering(self):
        self.breaks(die=1)
        with mock.patch.object(time, "sleep") as sleep:
            self.client.worker(self.spec())
        sleep.assert_called_once_with(1)
        self.assertEqual(self.runs(), 2)
        os.unlink(self.record)
        os.unlink(self.app + ".runs")
        self.breaks(die=2)
        with mock.patch.object(time, "sleep"):
            self.assertEqual(self.fails(), "codex hooks/list: app-server exited 1 before answering")
        self.assertEqual(self.runs(), 2)

    def test_no_answer_in_20_s_kills_its_process_group(self):
        """Its stdin closed, it neither exits nor closes stdout within 5 s: its process group is killed."""
        self.breaks(deaf=True)
        with virtual_time():
            self.assertEqual(self.fails(), "codex hooks/list: no answer in 20 s")
        with open(self.app + ".pids") as f:
            pids = json.load(f)
        for pid in pids:
            for _ in range(100):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"{pid} outlived the kill")

    def test_one_discovery_per_codex_home_at_a_time(self):
        self.make_record()
        lock = os.path.join(self.workers, hashlib.sha256(self.codex_home.encode()).hexdigest()[:16] + ".lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with virtual_time():
            self.assertEqual(self.fails(resume=True),
                             f"codex hooks/list: another Codex worker's start held {lock} for 30 s")
        self.assertEqual(self.runs(), 0)
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.client.worker(self.spec(resume=True))
        self.assertEqual(self.runs(), 1)

    def test_worker_reply(self):
        self.assertEqual(self.fails_reply(), f"no Codex worker record for {SID}")
        self.make_record(None)
        self.assertEqual(self.fails_reply(), UNBOUND)
        self.make_record()
        self.assertEqual(self.client.worker_reply(SID), "")
        reply = os.path.join(self.workers, f"{SID}.reply")
        fd = os.open(reply, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("**Done**: 完成\nline 2")
        self.assertEqual(self.client.worker_reply(SID), "**Done**: 完成\nline 2")
        os.chmod(reply, 0o644)
        self.assertEqual(self.fails_reply(), f"Codex worker reply {reply}: not a 0600 regular file of yours")
        os.unlink(reply)
        os.symlink(self.record, reply)
        self.assertRegex(self.fails_reply(), f"^{re.escape(f'Codex worker reply {reply}: ')}.*symbolic links")
        self.assertEqual(self.fails_reply("../x"), "bad session id '../x'")

    def fails_reply(self, sid=SID):
        with self.assertRaises(ConfigError) as cm:
            self.client.worker_reply(sid)
        return str(cm.exception)


class WorkerAttributes(unittest.TestCase):
    def test_marker_context_variables_and_dialogs(self):
        cls = codex.CodexClient
        self.assertEqual(cls.session_env, "CODEX_THREAD_ID")
        self.assertEqual(dict(cls.command_env), {
            **dict.fromkeys(("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_VERSION", "CODEX_SANDBOX",
                             "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_PERMISSION_PROFILE")),
            "NO_COLOR": "1", "CODEX_CI": "1", "PAGER": "cat", "GIT_PAGER": "cat", "GH_PAGER": "cat"})
        self.assertEqual(cls.worker_dialogs, (
            "Trust this folder?", "Config, hooks, and exec policies from untrusted folders stay disabled.",
            "Hooks need review", "Working directory · resume", "This conversation is open in another app",
            "Sign in with ChatGPT to use Codex"))
        with self.assertRaises(TypeError):
            cls.command_env["NO_COLOR"] = "0"


if __name__ == "__main__":
    unittest.main()
