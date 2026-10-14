"""Session behavior tests with caller-owned dependencies."""

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

from agent_pm.session.engine import types as engine_types
from agent_pm.session.modes import tui_hook

from session.tests.case import SRC, Case
from session.tests.tui_hook_support import HOOK, SOURCE_ROOTS, identity, install, state, write_state
from utils.tests.process_fixtures import DEVNULL

RUNTIME = re.compile(r"claude|codex", re.I)


class InProcess(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.dir = os.path.join(self.root, "s1")
        os.mkdir(self.dir, 0o700)
        self.events = os.path.join(self.root, "events")
        self.tmux = install(os.path.join(self.root, "bin"), "")
        self.state = state(self.root, self.events)
        write_state(self.dir, self.state)
        self.lookup = f"%3\t{identity(self.state)}\n"
        self.mark = unittest.mock.Mock(
            side_effect=[
                unittest.mock.Mock(returncode=0, stdout=self.lookup, stderr=""),
                unittest.mock.Mock(returncode=0, stdout="", stderr=""),
            ]
        )

    def faked(self, uid=None, mode=None, *, executable=True):
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
            unittest.mock.patch("subprocess.run", self.mark),
        ):
            tui_hook.main([self.dir, SOURCE_ROOTS, "tool"], io.BytesIO(b"{}"))

    def test_a_tmux_of_yours_or_roots_marks(self):
        for uid in (os.getuid(), 0):
            with self.subTest(uid=uid):
                self.mark.reset_mock(side_effect=True)
                self.mark.side_effect = [
                    unittest.mock.Mock(returncode=0, stdout=self.lookup, stderr=""),
                    unittest.mock.Mock(returncode=0, stdout="", stderr=""),
                ]
                self.faked(uid=uid)
                self.assertEqual(
                    self.mark.call_args.args[0],
                    [self.tmux, "set-option", "-t", "%3", "@state", "working"],
                )
                self.assertEqual(self.mark.call_count, 2)

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
        self.mark.assert_not_called()
        with open(os.path.join(self.dir, "hooks.jsonl"), "rb") as f:
            self.assertEqual(len(f.read().splitlines()), 3)

    def test_one_write_under_an_exclusive_flock_then_the_mark(self):
        calls = unittest.mock.Mock()
        with (
            unittest.mock.patch("fcntl.flock", wraps=fcntl.flock) as flock,
            unittest.mock.patch("os.write", wraps=os.write) as write,
            unittest.mock.patch("subprocess.run", self.mark),
        ):
            calls.attach_mock(flock, "flock")
            calls.attach_mock(write, "write")
            calls.attach_mock(self.mark, "mark")
            tui_hook.main(
                [self.dir, SOURCE_ROOTS, "tool"],
                io.BytesIO(b'{"tool_name": "Bash"}'),
            )
        with open(os.path.join(self.dir, "hooks.jsonl"), "rb") as f:
            data = f.read()
        fd = calls.mock_calls[0].args[0]
        self.assertEqual(
            calls.mock_calls,
            [
                unittest.mock.call.flock(fd, fcntl.LOCK_EX),
                unittest.mock.call.write(fd, data),
                unittest.mock.call.mark(
                    [
                        self.tmux,
                        "display-message",
                        "-p",
                        "-t",
                        "=w1:",
                        "#{pane_id}\t#{@sid}\t#{@generation}",
                    ],
                    capture_output=True,
                    text=True,
                    stdin=DEVNULL,
                    timeout=5,
                    env={
                        **{
                            key: value
                            for key, value in os.environ.items()
                            if key not in ("TMUX", "TMUX_TMPDIR")
                        },
                        "TMUX_TMPDIR": self.root,
                    },
                ),
                unittest.mock.call.mark(
                    [self.tmux, "set-option", "-t", "%3", "@state", "working"],
                    capture_output=True,
                    text=True,
                    stdin=DEVNULL,
                    timeout=5,
                    env={
                        **{
                            key: value
                            for key, value in os.environ.items()
                            if key not in ("TMUX", "TMUX_TMPDIR")
                        },
                        "TMUX_TMPDIR": self.root,
                    },
                ),
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


class Standalone(Case):
    def setUp(self):
        with open(HOOK) as f:
            self.source = f.read()

    def test_imports_only_the_stdlib_before_bootstrapping_shared_types(self):
        names, shared = set(), []
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.module == "agent_pm.session.engine.types":
                    shared.append([alias.name for alias in node.names])
                else:
                    names.add("." * node.level + (node.module or "").split(".")[0])
        self.assertLessEqual(names, set(sys.stdlib_module_names))
        self.assertEqual(shared, [["HookError", "Word"], ["HookError", "Word"]])

    def test_no_runtime_name(self):
        self.assertEqual(RUNTIME.findall(self.source), [])


class WordTest(Case):
    def test_the_words_a_label_may_mark(self):
        self.assertEqual(list(tui_hook.Word), ["done", "blocked", "working"])
        check = tui_hook.SESSION_STATE["words"]
        self.assertTrue(check({"stop": tui_hook.Word.DONE, "tool": "working"}))
        for bad in ({"stop": "dead"}, {"stop": ["done"]}, {"stop": None}):
            with self.subTest(words=bad):
                self.assertFalse(check(bad))


class SharedTypes(Case):
    """The bootstrapped hook uses engine/types' HookError and Word."""

    def test_the_hook_uses_engine_types(self):
        self.assertEqual(
            [(w.name, w.value) for w in tui_hook.Word],
            [(w.name, w.value) for w in engine_types.Word],
        )
        hook, shared = tui_hook.HookError, engine_types.HookError
        self.assertIs(hook, shared)
        self.assertIs(tui_hook.Word, engine_types.Word)
        self.assertEqual(
            (hook.__name__, hook.__bases__, hook.__doc__),
            (shared.__name__, shared.__bases__, shared.__doc__),
        )

    def test_each_shared_hook_type_has_one_definition(self):
        package = os.path.join(SRC["session"], "agent_pm", "session")
        definitions = {"Word": [], "HookError": []}
        for top, _, names in os.walk(package):
            for name in sorted(n for n in names if n.endswith(".py")):
                path = os.path.join(top, name)
                for node in ast.walk(ast.parse(self.source(path))):
                    if isinstance(node, ast.ClassDef) and node.name in definitions:
                        definitions[node.name].append(path)
        self.assertEqual(
            definitions,
            dict.fromkeys(definitions, [engine_types.__file__]),
        )

    def test_read_state_raises_the_error_it_is_given(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "session.json")
            with self.assertRaises(engine_types.HookError) as caught:
                tui_hook.read_state(path, error=engine_types.HookError)
            self.assertEqual(str(caught.exception), f"{path}: No such file or directory")
            with self.assertRaises(tui_hook.HookError):
                tui_hook.read_state(path)

    def test_no_normal_session_module_takes_a_type_from_the_hook(self):
        classes = {"HookError", "Word"}
        package = os.path.join(SRC["session"], "agent_pm", "session")
        found = []
        for top, _, names in os.walk(package):
            for name in sorted(n for n in names if n.endswith(".py")):
                path = os.path.join(top, name)
                if path != HOOK:
                    found += self.hook_types(path, classes)
        self.assertEqual(found, [])

    @staticmethod
    def source(path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read()

    def hook_types(self, path: str, classes: set[str]) -> list[str]:
        tree, bound, found = ast.parse(self.source(path)), set(), []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "agent_pm.session.modes":
                bound |= {a.asname or a.name for a in node.names if a.name == "tui_hook"}
            elif isinstance(node, ast.ImportFrom) and node.module == tui_hook.__name__:
                found += [f"{path}: {a.name}" for a in node.names if a.name in classes]
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in bound
                and node.attr in classes
            ):
                found.append(f"{path}:{node.lineno}: tui_hook.{node.attr}")
        return found
