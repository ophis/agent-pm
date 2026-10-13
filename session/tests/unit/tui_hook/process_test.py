"""In-process and standalone hook behaviour."""

import ast
import fcntl
import io
import json
import os
import re
import stat
import sys
import tempfile
import types
import unittest
import unittest.mock

from agent_pm.session.modes import tui_hook

from session.tests.case import SRC, Case
from session.tests.unit.tui_hook.case import HOOK, RUNTIME, install, state, write_state


class InProcess(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.dir = os.path.join(self.root, "s1")
        os.mkdir(self.dir, 0o700)
        self.events = os.path.join(self.root, "events")
        self.tmux = install(os.path.join(self.root, "bin"), "")
        write_state(self.dir, state(self.root, self.events))
        self.fake = types.SimpleNamespace(mark=unittest.mock.Mock(), TuiError=Exception)

    def faked(self, uid=None, mode=None, *, executable=True):
        """main on a tool record, tmux_host the fake; os.stat and os.access of the tmux faked: owner
        uid, st_mode mode (None: the real ones), whether executable.
        """
        real_stat, real_access = os.stat, os.access

        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if path != self.tmux:
                return st
            return types.SimpleNamespace(
                st_mode=st.st_mode if mode is None else mode,
                st_uid=st.st_uid if uid is None else uid,
            )

        def fake_access(path, how, *args, **kwargs):
            return executable if path == self.tmux else real_access(path, how, *args, **kwargs)

        with (
            unittest.mock.patch("os.stat", fake_stat),
            unittest.mock.patch("os.access", fake_access),
        ):
            tui_hook.main([self.dir, "tool"], io.BytesIO(b"{}"), load_host=lambda: self.fake)

    def test_a_tmux_of_yours_or_roots_marks(self):
        for uid in (os.getuid(), 0):
            with self.subTest(uid=uid):
                self.fake.mark.reset_mock()
                self.faked(uid=uid)
                self.fake.mark.assert_called_once_with("working", self.events, tmux=self.tmux)

    def test_a_foreign_non_regular_or_non_executable_tmux_is_refused_after_the_record(self):
        for case, kwargs in (
            ("foreign", {"uid": os.getuid() + 1}),
            ("directory", {"mode": stat.S_IFDIR | 0o755}),
            ("not executable", {"executable": False}),
        ):
            with (
                self.subTest(case),
                self.assertRaisesRegex(tui_hook.HookError, re.escape(self.tmux)),
            ):
                self.faked(**kwargs)
        self.fake.mark.assert_not_called()
        with open(os.path.join(self.dir, "hooks.jsonl"), "rb") as f:
            self.assertEqual(len(f.read().splitlines()), 3)

    def test_one_write_under_an_exclusive_flock_then_the_mark(self):
        fake = self.fake
        calls = unittest.mock.Mock()
        with (
            unittest.mock.patch("fcntl.flock", wraps=fcntl.flock) as flock,
            unittest.mock.patch("os.write", wraps=os.write) as write,
        ):
            calls.attach_mock(flock, "flock")
            calls.attach_mock(write, "write")
            calls.attach_mock(fake.mark, "mark")
            tui_hook.main(
                [self.dir, "tool"], io.BytesIO(b'{"tool_name": "Bash"}'), load_host=lambda: fake
            )
        with open(os.path.join(self.dir, "hooks.jsonl"), "rb") as f:
            data = f.read()
        fd = calls.mock_calls[0].args[0]
        self.assertEqual(
            calls.mock_calls,
            [
                unittest.mock.call.flock(fd, fcntl.LOCK_EX),
                unittest.mock.call.write(fd, data),
                unittest.mock.call.mark("working", self.events, tmux=self.tmux),
            ],
        )
        self.assertEqual(json.loads(data)["payload"], {"tool_name": "Bash"})

    def test_read_state_of_no_regular_file_leaves_no_fd_open(self):
        path = os.path.join(self.dir, "session.json")
        os.unlink(path)
        for make in (lambda: os.mkdir(path), lambda: os.mkfifo(path)):
            make()
            with self.subTest(stat.filemode(os.lstat(path).st_mode)):
                before = len(os.listdir("/dev/fd"))
                for _ in range(5):
                    with self.assertRaises(tui_hook.HookError):
                        tui_hook.read_state(path)
                self.assertEqual(len(os.listdir("/dev/fd")), before)
            (os.rmdir if os.path.isdir(path) else os.unlink)(path)

    def test_tmux_host_is_loaded_by_its_path_with_no_sys_path_change(self):
        path, modules = list(sys.path), set(sys.modules)
        module = tui_hook.load_tmux_host()
        self.assertEqual(
            os.path.realpath(module.__file__),
            os.path.realpath(
                os.path.join(SRC["session"], "agent_pm", "session", "hosts", "tmux", "tmux_host.py")
            ),
        )
        self.assertTrue(callable(module.mark))
        self.assertEqual(sys.path, path)
        self.assertNotIn("tmux_host", set(sys.modules) - modules)


class Standalone(Case):
    def setUp(self):
        with open(HOOK) as f:
            self.source = f.read()

    def test_imports_only_the_stdlib(self):
        names = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add("." * node.level + (node.module or "").split(".")[0])
        self.assertLessEqual(names, set(sys.stdlib_module_names))

    def test_no_runtime_name(self):
        self.assertEqual(RUNTIME.findall(self.source), [])
