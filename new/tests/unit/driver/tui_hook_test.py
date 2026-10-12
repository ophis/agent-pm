"""modes/tui_hook.py run as a runtime runs a hook: `<python> -I tui_hook.py <dir> <label>`, the payload on
stdin, $TMUX_PANE set, session.json's tmux_path a stub that logs each call, PATH only a decoy tmux (no tmux runs
here); in process for its one locked write, its check of that tmux and its load of tmux_host; and the file standing
alone."""

import ast
import fcntl
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
import uuid

from agent_pm.session.modes import tui_hook

from new.tests import guards
from new.tests.case import ROOT, Case

HOOK = os.path.join(ROOT, "agent_pm", "session", "modes", "tui_hook.py")
PANE = "%3"
LINE = r"\A[0-9]{2}:[0-9]{2}:[0-9]{2} w1 %s\n\Z"  # one event line
TS = r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d\Z"
WORDS = {"stop": "done", "ask": "blocked", "tool": "working"}
MISSING = object()
TMUX = """#!{python} -I
import json, os, sys
with open(os.environ["TMUX_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("TMUX_FAIL"):
    sys.exit("boom\\nagain")
if sys.argv[1] == "display-message":
    print("w1")
"""
DECOY = """#!{python} -I
open({log!r}, "a").close()
"""


def install(folder: str, text: str) -> str:
    os.mkdir(folder)
    path = os.path.join(folder, "tmux")
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


def state(root: str, events) -> dict:
    return {
        "runtime": "fake",
        "sid": str(uuid.uuid4()),
        "workdir": root,
        "cwd": root,
        "project": False,
        "transcript": "",
        "resume": "",
        "events": events,
        "words": dict(WORDS),
        "tmux_path": os.path.join(root, "bin", "tmux"),
    }


def write_state(folder: str, value) -> None:
    data = value if isinstance(value, bytes) else json.dumps(value).encode()
    fd = os.open(os.path.join(folder, "session.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


class Hook(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.dir = os.path.join(self.root, "s1")
        os.mkdir(self.dir, 0o700)
        self.hooks = os.path.join(self.dir, "hooks.jsonl")
        self.events = os.path.join(self.root, "events")
        os.close(os.open(self.events, os.O_WRONLY | os.O_CREAT, 0o600))
        self.tmux = install(os.path.join(self.root, "bin"), TMUX.format(python=sys.executable))
        self.decoy = os.path.join(self.root, "decoy.log")
        install(
            os.path.join(self.root, "path"), DECOY.format(python=sys.executable, log=self.decoy)
        )
        self.log = os.path.join(self.root, "tmux.log")
        self.env = {
            "PATH": os.path.join(self.root, "path"),
            "TMUX_PANE": PANE,
            "TMUX_LOG": self.log,
            "TMUX_TMPDIR": self.root,
            "HOME": self.root,
        }
        self.state = state(self.root, self.events)
        write_state(self.dir, self.state)

    def hook(self, label, payload=None, args=None, **env):
        """Runs the hook (env: changes to self.env, None drops a key); checks it exits 0 printing nothing on stdout;
        returns its stderr."""
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps({} if payload is None else payload).encode()
        )
        environ = {k: v for k, v in {**self.env, **env}.items() if v is not None}
        res = subprocess.run(
            [sys.executable, "-I", HOOK, *([self.dir, label] if args is None else args)],
            input=data,
            capture_output=True,
            env=environ,
            timeout=60,
        )
        self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)
        return res.stderr.decode()

    def assertError(self, err, text):
        """err is one stderr line holding text."""
        self.assertRegex(err, r"\Atui_hook: [^\n]*\n\Z")
        self.assertIn(text, err)

    def assertNothingWritten(self):
        self.assertFalse(os.path.lexists(self.hooks))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.lines(), "")

    def records(self):
        with open(self.hooks, "rb") as f:
            lines = f.read().split(b"\n")
        self.assertEqual(lines[-1], b"")
        return [json.loads(line) for line in lines[:-1]]

    def calls(self):
        try:
            with open(self.log) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def lines(self):
        with open(self.events) as f:
            return f.read()

    def reset(self):
        open(self.events, "w").close()
        if os.path.exists(self.log):
            os.unlink(self.log)

    def test_each_call_appends_one_record_line_ts_label_payload(self):
        payload = {"session_id": "s1", "text": "a\nb\u2028c\U0001f600", "n": [1, None, True, 2.5]}
        self.assertEqual(self.hook("stop", payload), "")
        self.assertEqual(self.hook("tool", {"tool_name": "Bash"}), "")
        records = self.records()
        self.assertEqual([list(r) for r in records], [["ts", "label", "payload"]] * 2)
        self.assertEqual(
            [(r["label"], r["payload"]) for r in records],
            [("stop", payload), ("tool", {"tool_name": "Bash"})],
        )
        for record in records:
            self.assertRegex(record["ts"], TS)
        self.assertEqual(stat.S_IMODE(os.stat(self.hooks).st_mode), 0o600)

    def test_each_word_marks_the_pane_done_and_blocked_with_an_event_line(self):
        for label, word in WORDS.items():
            with self.subTest(label):
                self.reset()
                self.assertEqual(self.hook(label), "")
                calls = [["set-option", "-t", PANE, "@state", word]]
                if word == "working":
                    self.assertEqual(self.lines(), "")
                else:
                    calls.append(["display-message", "-p", "-t", PANE, "#S"])
                    self.assertRegex(self.lines(), LINE % word)
                self.assertEqual(self.calls(), calls)
        self.assertEqual([r["label"] for r in self.records()], list(WORDS))

    def test_the_tmux_session_json_names_runs_never_one_on_path(self):
        self.assertEqual(self.hook("ask"), "")
        self.assertEqual(
            self.calls(),
            [
                ["set-option", "-t", PANE, "@state", "blocked"],
                ["display-message", "-p", "-t", PANE, "#S"],
            ],
        )
        self.assertRegex(self.lines(), LINE % "blocked")
        self.assertFalse(os.path.exists(self.decoy))

    def test_a_tmux_that_is_no_regular_executable_file_is_one_stderr_line_after_the_record(self):
        plain = os.path.join(self.root, "plain")
        with open(plain, "w") as f:
            f.write(TMUX.format(python=sys.executable))
        os.chmod(plain, 0o644)
        for tmux in (plain, os.path.join(self.root, "bin"), os.path.join(self.root, "missing")):
            with self.subTest(tmux):
                write_state(self.dir, {**self.state, "tmux_path": tmux})
                self.assertError(self.hook("stop"), tmux)
        self.assertEqual([r["label"] for r in self.records()], ["stop"] * 3)
        self.assertEqual((self.calls(), self.lines()), ([], ""))
        self.assertFalse(os.path.exists(self.decoy))

    def test_a_label_without_a_word_is_recorded_and_marks_nothing(self):
        self.assertEqual(self.hook("other", {"x": 1}), "")
        self.assertEqual(
            [(r["label"], r["payload"]) for r in self.records()], [("other", {"x": 1})]
        )
        self.assertEqual(self.calls(), [])

    def test_no_events_file_marks_the_state_with_no_line(self):
        write_state(self.dir, {**self.state, "events": None})
        for label, word in WORDS.items():
            with self.subTest(label):
                self.reset()
                self.assertEqual(self.hook(label), "")
                self.assertEqual(self.calls(), [["set-option", "-t", PANE, "@state", word]])
        self.assertEqual(self.lines(), "")
        self.assertEqual(len(self.records()), 3)

    def test_no_tmux_pane_records_and_marks_nothing(self):
        for pane in (None, ""):
            self.assertEqual(self.hook("stop", TMUX_PANE=pane), "")
        self.assertEqual(len(self.records()), 2)
        self.assertEqual((self.calls(), self.lines()), ([], ""))

    def test_valid_edge_states_are_accepted(self):
        write_state(
            self.dir,
            {
                **self.state,
                "words": {},
                "project": True,
                "cwd": "",
                "transcript": "/t.jsonl",
                "resume": "abc",
                "later": [1],
            },
        )
        self.assertEqual(self.hook("stop"), "")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.calls(), [])

    def test_strs_over_4096_chars_are_cut_unless_the_word_is_done(self):
        long = "\u00e9" * 5000
        key = "k" * 5000
        payload = {"a": long, "b": [{"c": "y" * 4097}, "z" * 4096], "n": 5, key: "v"}
        cut = {"a": long[:4096], "b": [{"c": "y" * 4096}, "z" * 4096], "n": 5, key: "v"}
        for label in ("tool", "ask", "other", "stop"):
            self.assertEqual(self.hook(label, payload), "")
        self.assertEqual([r["payload"] for r in self.records()], [cut, cut, cut, payload])

    def test_bad_stdin_is_an_empty_payload_and_a_stderr_line(self):
        cases = (
            b"",
            b"not json",
            b"[1]",
            b'"text"',
            b"\xff\xfe\xfd",
            b"[" * 100000,
            b'{"a": "' + b"x" * (16 << 20) + b'"}',
        )
        for data in cases:
            with self.subTest(data[:20]):
                self.assertError(self.hook("tool", data), "payload")
        self.assertEqual([r["payload"] for r in self.records()], [{}] * len(cases))

    def test_a_payload_of_16_mib_is_read(self):
        data = b'{"a": "' + b"x" * ((16 << 20) - 9) + b'"}'
        self.assertEqual(len(data), 16 << 20)
        self.assertEqual(self.hook("tool", data), "")
        self.assertEqual([r["payload"] for r in self.records()], [{"a": "x" * 4096}])

    def test_bad_arguments_write_nothing(self):
        for args in (
            [],
            [self.dir],
            [self.dir, "stop", "x"],
            ["s1", "stop"],
            [self.dir + "\n", "stop"],
            [self.dir, ""],
            [self.dir, "a b"],
            [self.dir, "x" * 65],
            [self.dir, "../x"],
        ):
            with self.subTest(args):
                self.assertError(self.hook(None, args=args), "usage")
        self.assertNothingWritten()

    def test_a_bad_session_dir_writes_nothing(self):
        afile = os.path.join(self.root, "file")
        open(afile, "w").close()
        for folder in (os.path.join(self.root, "missing"), afile):
            with self.subTest(folder):
                self.assertError(self.hook(None, args=[folder, "stop"]), "session.json")
        self.assertNothingWritten()

    def test_a_session_json_that_is_no_regular_file_writes_nothing(self):
        path = os.path.join(self.dir, "session.json")
        real = os.path.join(self.root, "real")
        os.mkdir(real)
        write_state(real, self.state)
        os.unlink(path)
        self.assertError(self.hook("stop"), "session.json")
        for make in (
            lambda: os.symlink(os.path.join(real, "session.json"), path),
            lambda: os.mkfifo(path),
            lambda: os.mkdir(path),
        ):
            make()
            with self.subTest(stat.filemode(os.lstat(path).st_mode)):
                self.assertError(self.hook("stop"), "session.json")
            (os.rmdir if os.path.isdir(path) and not os.path.islink(path) else os.unlink)(path)
        self.assertNothingWritten()

    def test_each_invalid_session_state_field_writes_nothing(self):
        bad = {
            "words": (
                [],
                "x",
                None,
                {"a b": "done"},
                {"": "done"},
                {"x" * 65: "done"},
                {"a/b": "done"},
                {"stop": "dead"},
                {"stop": None},
                {"stop": ["done"]},
            ),
            "events": ("", "events", "/a\nb", 1, []),
            "sid": ("", "a/b", "a\tb", 1, None),
            "runtime": ("", "Fake", "1x", "a-b", "a b", None),
            "workdir": ("", "rel", "/a\x00b", None),
            "cwd": ("rel", "/a\nb", None),
            "transcript": ("rel", "/a\x7fb", None),
            "project": ("true", 0, 1, None),
            "resume": (None, 1, []),
            "tmux_path": ("", "tmux", "bin/tmux", "/a\nb", None, 1),
        }
        for field, values in bad.items():
            for value in (*values, MISSING):
                with self.subTest(field=field, value=value):
                    changed = {k: v for k, v in self.state.items() if k != field}
                    if value is not MISSING:
                        changed[field] = value
                    write_state(self.dir, changed)
                    self.assertError(self.hook("stop"), "session.json")
                    self.assertNothingWritten()

    def test_a_session_json_not_an_object_of_at_most_1_mib_writes_nothing(self):
        for data in (
            b"",
            b"not json",
            b"[]",
            b'"s"',
            b"\xff",
            json.dumps({**self.state, "pad": "x" * (1 << 20)}).encode(),
        ):
            with self.subTest(data[:20]):
                write_state(self.dir, data)
                self.assertError(self.hook("stop"), "session.json")
        self.assertNothingWritten()

    def test_hooks_jsonl_that_is_no_regular_file_is_refused_and_nothing_marked(self):
        target = os.path.join(self.root, "target")
        open(target, "w").close()
        for make in (
            lambda: os.symlink(target, self.hooks),
            lambda: os.mkfifo(self.hooks),
            lambda: os.mkdir(self.hooks),
        ):
            make()
            with self.subTest(stat.filemode(os.lstat(self.hooks).st_mode)):
                self.assertError(self.hook("stop"), "hooks.jsonl")
            (
                os.rmdir
                if os.path.isdir(self.hooks) and not os.path.islink(self.hooks)
                else os.unlink
            )(self.hooks)
        with open(target) as f:
            self.assertEqual(f.read(), "")
        self.assertNothingWritten()

    def test_a_failed_mark_is_one_stderr_line_after_the_record(self):
        self.assertError(self.hook("stop", TMUX_FAIL="1"), "tmux: boom again")
        os.unlink(self.events)
        self.assertError(self.hook("ask"), "events file")
        self.assertFalse(os.path.exists(self.events))
        self.assertEqual([r["label"] for r in self.records()], ["stop", "ask"])

    def test_an_unexpected_error_still_exits_0(self):
        res = subprocess.run(
            [sys.executable, "-I", HOOK, self.dir, "stop"],
            capture_output=True,
            env=self.env,
            timeout=60,
            preexec_fn=lambda: os.close(0),
        )
        self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)
        self.assertError(res.stderr.decode(), "Error")
        self.assertNothingWritten()


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

    def faked(self, uid=None, mode=None, executable=True):
        """main on a tool record, tmux_host the fake; os.stat and os.access of the tmux faked: owner uid, st_mode mode
        (None: the real ones), whether executable."""
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
            unittest.mock.patch.object(tui_hook, "load_tmux_host", return_value=self.fake),
            unittest.mock.patch("os.stat", fake_stat),
            unittest.mock.patch("os.access", fake_access),
        ):
            tui_hook.main([self.dir, "tool"], io.BytesIO(b"{}"))

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
            with self.subTest(case):
                with self.assertRaisesRegex(tui_hook.HookError, re.escape(self.tmux)):
                    self.faked(**kwargs)
        self.fake.mark.assert_not_called()
        with open(os.path.join(self.dir, "hooks.jsonl"), "rb") as f:
            self.assertEqual(len(f.read().splitlines()), 3)

    def test_one_write_under_an_exclusive_flock_then_the_mark(self):
        fake = self.fake
        calls = unittest.mock.Mock()
        with (
            unittest.mock.patch.object(tui_hook, "load_tmux_host", return_value=fake),
            unittest.mock.patch("fcntl.flock", wraps=fcntl.flock) as flock,
            unittest.mock.patch("os.write", wraps=os.write) as write,
        ):
            calls.attach_mock(flock, "flock")
            calls.attach_mock(write, "write")
            calls.attach_mock(fake.mark, "mark")
            tui_hook.main([self.dir, "tool"], io.BytesIO(b'{"tool_name": "Bash"}'))
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
                os.path.join(ROOT, "agent_pm", "session", "hosts", "tmux", "tmux_host.py")
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

    def test_python_3_9_syntax(self):
        ast.parse(self.source, feature_version=(3, 9))

    def test_no_runtime_name(self):
        self.assertEqual(guards.RUNTIME.findall(self.source), [])


if __name__ == "__main__":
    unittest.main()
