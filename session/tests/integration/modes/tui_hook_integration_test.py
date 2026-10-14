"""modes/tui_hook.py run by path as tmux runs it."""

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr

from agent_pm.session.modes import tui_hook

from session.tests.case import SRC, Case
from session.tests.source_roots import source_roots_cases
from session.tests.tui_hook_support import (
    HOOK,
    SOURCE_ROOTS,
    WORDS,
    descriptor,
    identity,
    install,
    state,
    write_state,
)

PANE = "%3"
LINE = r"\A[0-9]{2}:[0-9]{2}:[0-9]{2} w1 %s\n\Z"  # one event line
TS = r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d\Z"
MISSING = object()
LOOKUP = ["display-message", "-p", "-t", "=w1:", "#{pane_id}\t#{@sid}\t#{@generation}"]
TMUX = """#!{python} -I
import json, os, sys
with open(os.environ["TMUX_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("TMUX_FAIL"):
    sys.exit("boom\\nagain")
if sys.argv[1] == "display-message":
    if "pane_id" in sys.argv[-1]:
        print("" if os.environ.get("TMUX_TARGET_ABSENT") else "%3\\t" + os.environ["TMUX_TARGET_IDENTITY"])
    else:
        print("w1")
"""
DECOY = """#!{python} -I
open({log!r}, "a").close()
"""


class HookBase(Case):
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
        self.env["TMUX_TARGET_IDENTITY"] = identity(self.state)
        write_state(self.dir, self.state)

    def hook(self, label, payload=None, args=None, **env):
        """Runs the hook (env: changes to self.env, None drops a key); checks it exits 0 printing
        nothing on stdout; returns its stderr.
        """
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps({} if payload is None else payload).encode()
        )
        environ = {k: v for k, v in {**self.env, **env}.items() if v is not None}
        res = subprocess.run(
            [
                sys.executable,
                "-I",
                HOOK,
                *(
                    [self.dir, json.dumps([self.root, *json.loads(SOURCE_ROOTS)]), label]
                    if args is None
                    else args
                ),
            ],
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


class MarkingTest(HookBase):
    def test_a_failed_mark_is_one_stderr_line_after_the_record(self):
        self.assertError(self.hook("stop", TMUX_FAIL="1"), "tmux: boom again")
        os.unlink(self.events)
        self.assertError(self.hook("ask"), "events file")
        self.assertFalse(os.path.exists(self.events))
        self.assertEqual([r["label"] for r in self.records()], ["stop", "ask"])

    def test_disabled_logging_reports_exactly_one_error_line_and_exits_zero(self):
        script = """import logging, runpy, sys
logging.disable(logging.CRITICAL)
path = sys.argv[1]
sys.argv = [path]
runpy.run_path(path, run_name="__main__")
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, HOOK],
            input=b"{}",
            capture_output=True,
            env=self.env,
            check=True,
        )
        self.assertEqual(result.stdout, b"")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith(b"tui_hook: usage: tui_hook.py "))

    def test_an_unexpected_error_still_exits_0(self):
        res = subprocess.run(
            [sys.executable, "-I", HOOK, self.dir, SOURCE_ROOTS, "stop"],
            capture_output=True,
            env=self.env,
            timeout=60,
            preexec_fn=lambda: os.close(0),
        )
        self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)
        self.assertError(res.stderr.decode(), "Error")
        self.assertNothingWritten()

    def test_adapter_error_is_one_stderr_line_without_a_record_or_mark(self):
        callback = os.path.join(self.root, "adapter.py")
        with open(callback, "w") as file:
            file.write('def on_hook(call):\n    raise RuntimeError("first\\nsecond")\n')
        self.state["adapter_hook"] = descriptor("adapter")
        write_state(self.dir, self.state)
        self.assertEqual(
            self.hook("stop", {"text": "payload"}), "tui_hook: RuntimeError: first second\n"
        )
        self.assertNothingWritten()


class ErrorLoggingTest(HookBase):
    def test_package_import_failures_keep_a_traceback_and_one_error_line(self):
        package = os.path.join(self.root, "broken_package")
        os.mkdir(package)
        initializer = os.path.join(package, "__init__.py")
        with open(initializer, "w") as file:
            file.write('raise RuntimeError("first\\nsecond")\n')
        with open(os.path.join(package, "callback.py"), "w") as file:
            file.write("def on_hook(*args): pass\n")
        write_state(self.dir, {**self.state, "adapter_hook": descriptor("broken_package.callback")})
        self.assertEqual(self.hook("stop"), "tui_hook: RuntimeError: first second\n")
        with open(os.path.join(self.dir, "hook.log")) as file:
            trace = file.read()
        self.assertEqual(trace.count("Traceback (most recent call last):"), 1)
        self.assertIn(initializer, trace)
        self.assertIn("RuntimeError: first\nsecond\n", trace)
        self.assertNothingWritten()

    def test_adapter_failures_append_full_tracebacks_without_a_record_or_mark(self):
        callback = os.path.join(self.root, "adapter.py")
        with open(callback, "w") as file:
            file.write('def on_hook(call):\n    raise RuntimeError("first\\nsecond")\n')
        self.state["adapter_hook"] = descriptor("adapter")
        write_state(self.dir, self.state)
        error_log = os.path.join(self.dir, "hook.log")
        for count in (1, 2):
            self.assertEqual(self.hook("stop"), "tui_hook: RuntimeError: first second\n")
            with open(error_log) as file:
                trace = file.read()
            self.assertEqual(trace.count("Traceback (most recent call last):"), count)
            self.assertEqual(trace.count("RuntimeError: first\nsecond\n"), count)
            self.assertIn(callback, trace)
            self.assertIn("in on_hook", trace)
            self.assertNothingWritten()

    def test_unusable_or_invalid_session_dirs_keep_one_stderr_line(self):
        for folder in (
            os.path.join(self.root, "missing"),
            os.path.join(self.dir, "session.json"),
            "relative",
        ):
            with self.subTest(folder=folder):
                self.assertError(self.hook("stop", args=[folder, SOURCE_ROOTS, "stop"]), "tui_hook")
                self.assertFalse(os.path.exists(os.path.join(folder, "hook.log")))
        self.assertNothingWritten()

    def test_unusable_log_file_keeps_one_stderr_line(self):
        os.mkdir(os.path.join(self.dir, "hook.log"))
        os.unlink(os.path.join(self.dir, "session.json"))
        self.assertError(self.hook("stop"), "session.json")
        self.assertNothingWritten()

    def test_failed_file_write_keeps_one_stderr_line(self):
        script = """import builtins, io, os, runpy, sys
from unittest import mock
class BrokenFile(io.StringIO):
    def write(self, value):
        raise OSError("disk full")
path, folder, roots = sys.argv[1:]
sys.argv = [path, folder, roots, "stop"]
real_open = builtins.open
def open_file(name, *args, **kwargs):
    if os.fspath(name) == os.path.join(folder, "hook.log"):
        return BrokenFile()
    return real_open(name, *args, **kwargs)
with mock.patch("builtins.open", side_effect=open_file):
    runpy.run_path(path, run_name="__main__")
"""
        os.unlink(os.path.join(self.dir, "session.json"))
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, HOOK, self.dir, SOURCE_ROOTS],
            input=b"{}",
            capture_output=True,
            env=self.env,
            check=True,
        )
        self.assertEqual(result.stdout, b"")
        self.assertError(result.stderr.decode(), "session.json")
        self.assertNothingWritten()

    def test_success_keeps_the_journal_and_mark_without_an_error_log(self):
        self.assertEqual(self.hook("stop", {"text": "success"}), "")
        self.assertFalse(os.path.lexists(os.path.join(self.dir, "hook.log")))
        self.assertEqual(self.records()[0]["payload"], {"text": "success"})
        self.assertEqual(self.calls()[:2], [LOOKUP, ["set-option", "-t", PANE, "@state", "done"]])
        self.assertRegex(self.lines(), LINE % "done")


class PayloadTest(HookBase):
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
        )
        for data in cases:
            with self.subTest(data[:20]):
                self.assertError(self.hook("tool", data), "payload")
        self.assertEqual([r["payload"] for r in self.records()], [{}] * len(cases))

    def test_a_payload_of_64_mib_is_read(self):
        data = b'{"a": "' + b"x" * ((64 << 20) - 9) + b'"}'
        self.assertEqual(len(data), 64 << 20)
        callback = os.path.join(self.root, "adapter.py")
        with open(callback, "w") as file:
            file.write(
                'def on_hook(call):\n    return {**call.payload, "length": len(call.payload["a"])}\n'
            )
        self.state["adapter_hook"] = descriptor("adapter")
        write_state(self.dir, self.state)
        self.assertEqual(self.hook("tool", data), "")
        self.assertEqual(
            [r["payload"] for r in self.records()], [{"a": "x" * 4096, "length": (64 << 20) - 9}]
        )

    def test_overflow_drains_stdin_and_still_journals_and_marks(self):
        stream = io.BytesIO(b"x" * (tui_hook.PAYLOAD_MAX + 1))
        with (
            unittest.mock.patch.dict(os.environ, self.env),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            tui_hook.main([self.dir, SOURCE_ROOTS, "stop"], stream)
        self.assertEqual(
            stderr.getvalue(), f"tui_hook: payload: over {tui_hook.PAYLOAD_MAX} bytes\n"
        )
        self.assertEqual(stream.read(), b"")
        self.assertEqual(self.records()[0]["payload"], {})
        self.assertNotEqual(self.calls(), [])
        self.assertRegex(self.lines(), LINE % "done")

    def test_adapter_callback_receives_full_payload_before_truncation_and_can_skip(self):
        callback = os.path.join(self.root, "adapter.py")
        with open(callback, "w") as file:
            file.write(
                """def on_hook(call):
    if call.label == "ask":
        return None
    return {**call.payload, "original_length": len(call.payload.get("text", ""))}
"""
            )
        self.state["adapter_hook"] = descriptor("adapter")
        write_state(self.dir, self.state)
        self.assertEqual(self.hook("tool", {"text": "x" * 5000}), "")
        record = self.records()[0]
        self.assertEqual(record["payload"], {"text": "x" * 4096, "original_length": 5000})
        marked = self.calls()
        self.assertEqual(self.hook("ask", {"text": "skip"}), "")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.calls(), marked)

    def test_callback_module_requires_regular_owned_file_with_on_hook(self):
        callback = os.path.join(self.root, "adapter.py")
        with open(callback, "w") as file:
            file.write("value = 1\n")
        os.symlink(callback, os.path.join(self.root, "linked.py"))
        os.mkdir(os.path.join(self.root, "namespace"))
        for kind in ("adapter_hook", "host_hook"):
            for name in (
                "relative.py/",
                callback,
                "absent",
                "namespace",
                "sys",
                "linked",
                "adapter",
            ):
                with self.subTest(kind=kind, module=name):
                    write_state(self.dir, {**self.state, kind: descriptor(name)})
                    self.assertError(self.hook("stop"), kind.split("_")[0])
        self.assertNothingWritten()


class RecordsTest(HookBase):
    def test_host_callback_observes_the_journal_and_receives_the_mode_payload(self):
        callback = os.path.join(self.root, "host_callback.py")
        seen = os.path.join(self.root, "seen.jsonl")
        with open(callback, "w") as file:
            file.write(f"""import json
def on_hook(word, call):
    with open({self.hooks!r}) as journal:
        record = json.loads(journal.readlines()[-1])
    assert record["payload"] == call.payload
    with open({seen!r}, "a") as output:
        output.write(json.dumps([word, call.payload, dict(call.state)]) + "\\n")
""")
        write_state(self.dir, {**self.state, "host_hook": descriptor("host_callback", {"key": 3})})
        payload = {"text": "x" * 5000}
        for label in ("tool", "stop", "other"):
            self.assertEqual(self.hook(label, payload), "")
        with open(seen) as file:
            observed = [json.loads(line) for line in file]
        self.assertEqual(
            observed,
            [
                ["working", {"text": "x" * 4096}, {"key": 3}],
                ["done", payload, {"key": 3}],
            ],
        )
        self.assertEqual(len(self.records()), 3)
        self.assertEqual(self.calls(), [])

    def test_two_synchronized_writers_keep_every_complete_journal_record(self):
        script = """import io, json, sys
folder, encoded, writer = sys.argv[1:]
sys.path[:0] = json.loads(encoded)
from agent_pm.session.modes import tui_hook
print("ready", flush=True)
sys.stdin.buffer.read(1)
for number in range(20):
    payload = json.dumps({"writer": writer, "number": number}).encode()
    tui_hook.main([folder, encoded, "other"], io.BytesIO(payload))
"""
        processes = []
        for writer in ("first", "second"):
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", script, self.dir, SOURCE_ROOTS, writer],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
            )
            self.addCleanup(process.kill)
            self.addCleanup(process.wait)
            self.assertEqual(process.stdout.readline(), b"ready\n")
            processes.append(process)
        for process in processes:
            process.stdin.write(b"x")
            process.stdin.flush()
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual((process.returncode, stdout, stderr), (0, b"", b""))
        records = self.records()
        self.assertEqual(len(records), 40)
        self.assertEqual(
            {(r["payload"]["writer"], r["payload"]["number"]) for r in records},
            {(writer, number) for writer in ("first", "second") for number in range(20)},
        )
        self.assertEqual(self.calls(), [])

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
                calls = [LOOKUP, ["set-option", "-t", PANE, "@state", word]]
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
                LOOKUP,
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
                host = {**self.state["host_hook"], "state": {"executable": tmux}}
                write_state(self.dir, {**self.state, "host_hook": host})
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
                self.assertEqual(self.calls(), [LOOKUP, ["set-option", "-t", PANE, "@state", word]])
        self.assertEqual(self.lines(), "")
        self.assertEqual(len(self.records()), 3)

    def test_target_lookup_works_without_runtime_pane_environment(self):
        for pane in (None, ""):
            self.assertEqual(self.hook("stop", TMUX_PANE=pane), "")
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(
            self.calls(),
            [
                LOOKUP,
                ["set-option", "-t", PANE, "@state", "done"],
                ["display-message", "-p", "-t", PANE, "#S"],
            ]
            * 2,
        )

    def test_missing_target_journals_without_marking(self):
        self.assertEqual(self.hook("stop", TMUX_TARGET_ABSENT="1"), "")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual((self.calls(), self.lines()), ([LOOKUP], ""))

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


class SecurityTest(HookBase):
    def test_invalid_arguments_and_roots_drain_a_large_pipe_before_exiting(self):
        data = b'{"data":"' + b"x" * (2 << 20) + b'"}'
        for arguments in ([], [self.dir, "not-json", "stop"]):
            with self.subTest(arguments=arguments):
                process = subprocess.Popen(
                    [sys.executable, "-I", HOOK, *arguments],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=self.env,
                )
                self.addCleanup(process.kill)
                self.addCleanup(process.wait)
                with process.stdin as stream:
                    self.assertEqual(stream.write(data), len(data))
                process.stdin = None
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual((process.returncode, stdout), (0, b""))
                self.assertError(stderr.decode(), "tui_hook")
        self.assertNothingWritten()

    def test_foreign_callback_is_checked_before_the_session_package_initializer(self):
        package = os.path.join(self.root, "agent_pm")
        os.mkdir(package)
        marker = os.path.join(self.root, "package-imported")
        with open(os.path.join(package, "__init__.py"), "w") as file:
            file.write(f"open({marker!r}, 'w').close()\n")
        callback = os.path.join(package, "callback.py")
        with open(callback, "w") as file:
            file.write("def on_hook(*args): pass\n")
        script = """import os, runpy, sys, types, unittest.mock
path, rejected, *arguments = sys.argv[1:]
original = os.lstat
def foreign_owner(filename):
    info = original(filename)
    return types.SimpleNamespace(st_mode=info.st_mode,
        st_uid=os.getuid() + 1 if filename == rejected else info.st_uid)
sys.argv = [path, *arguments]
with unittest.mock.patch("os.lstat", foreign_owner):
    runpy.run_path(path, run_name="__main__")
"""
        for kind in ("adapter_hook", "host_hook"):
            with self.subTest(kind=kind):
                write_state(self.dir, {**self.state, kind: descriptor("agent_pm.callback")})
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        script,
                        HOOK,
                        callback,
                        self.dir,
                        json.dumps([self.root, *json.loads(SOURCE_ROOTS)]),
                        "stop",
                    ],
                    input=b"{}",
                    capture_output=True,
                    env=self.env,
                    timeout=10,
                    check=True,
                )
                self.assertEqual(result.stdout, b"")
                self.assertError(result.stderr.decode(), "not a regular file owned by you")
                self.assertFalse(os.path.exists(marker))
                self.assertNothingWritten()

    def callback_package(self):
        folder = os.path.join(self.root, "callback_package", "nested")
        os.makedirs(folder)
        imported = os.path.join(self.root, "imported")
        files = [
            os.path.join(self.root, "callback_package", "__init__.py"),
            os.path.join(folder, "__init__.py"),
            os.path.join(folder, "callback.py"),
        ]
        for path in files:
            with open(path, "w") as file:
                file.write(f"with open({imported!r}, 'a') as marker: marker.write('imported\\n')\n")
                file.write(
                    "def on_hook(call):\n    return {**call.payload, 'callback_imported': True}\n"
                )
        self.state.update(adapter_hook=descriptor("callback_package.nested.callback"), words={})
        write_state(self.dir, self.state)
        return files, imported

    def test_callback_symlinks_are_rejected_before_any_package_code(self):
        files, imported = self.callback_package()
        for path in files:
            with self.subTest(path=path):
                target = path + ".target"
                os.rename(path, target)
                os.symlink(target, path)
                try:
                    self.assertError(self.hook("stop"), "not a regular file owned by you")
                    self.assertFalse(os.path.exists(imported))
                    self.assertNothingWritten()
                finally:
                    os.unlink(path)
                    os.rename(target, path)

    def test_foreign_callback_files_are_rejected_before_any_package_code(self):
        files, imported = self.callback_package()
        script = """import os, runpy, sys, types, unittest.mock
path, rejected, *arguments = sys.argv[1:]
original = os.lstat
def foreign_owner(filename):
    info = original(filename)
    return types.SimpleNamespace(
        st_mode=info.st_mode,
        st_uid=os.getuid() + 1 if filename == rejected else info.st_uid,
    )
sys.argv = [path, *arguments]
with unittest.mock.patch("os.lstat", foreign_owner):
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as error:
        assert error.code == 0
assert "callback_package" not in sys.modules
"""
        for rejected in files:
            with self.subTest(path=rejected):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        script,
                        HOOK,
                        rejected,
                        self.dir,
                        json.dumps([self.root, *json.loads(SOURCE_ROOTS)]),
                        "stop",
                    ],
                    input=b"{}",
                    capture_output=True,
                    env=self.env,
                    timeout=60,
                    check=True,
                )
                self.assertEqual(result.stdout, b"")
                self.assertError(result.stderr.decode(), "not a regular file owned by you")
                self.assertFalse(os.path.exists(imported))
                self.assertNothingWritten()

    def test_owned_callback_package_imports_after_the_complete_chain_is_checked(self):
        _, imported = self.callback_package()
        self.assertEqual(self.hook("stop"), "")
        self.assertEqual(self.records()[0]["payload"], {"callback_imported": True})
        with open(imported) as file:
            self.assertEqual(file.read(), "imported\n" * 3)

    def test_root_owned_callback_package_imports_after_the_complete_chain_is_checked(self):
        files, imported = self.callback_package()
        script = """import json, os, runpy, sys, types, unittest.mock
path, encoded_files, *arguments = sys.argv[1:]
files = json.loads(encoded_files)
original = os.lstat
def root_owner(filename):
    info = original(filename)
    return types.SimpleNamespace(
        st_mode=info.st_mode,
        st_uid=0 if filename in files else info.st_uid,
    )
sys.argv = [path, *arguments]
with unittest.mock.patch("os.lstat", root_owner):
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as error:
        assert error.code == 0
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                script,
                HOOK,
                json.dumps(files),
                self.dir,
                json.dumps([self.root, *json.loads(SOURCE_ROOTS)]),
                "stop",
            ],
            input=b"{}",
            capture_output=True,
            env=self.env,
            timeout=60,
            check=True,
        )
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(self.records()[0]["payload"], {"callback_imported": True})
        with open(imported) as file:
            self.assertEqual(file.read(), "imported\n" * 3)

    def test_nested_namespace_callback_imports_from_supplied_roots(self):
        sources = [os.path.join(self.root, name) for name in ("left", "right")]
        for source in sources:
            os.makedirs(os.path.join(source, "namespace", "nested"))
        callback = os.path.join(sources[1], "namespace", "nested", "callback.py")
        with open(callback, "w") as file:
            file.write(
                "def on_hook(call):\n    return {**call.payload, 'callback_imported': True}\n"
            )
        self.state.update(adapter_hook=descriptor("namespace.nested.callback"), words={})
        write_state(self.dir, self.state)
        roots = [*sources, *json.loads(SOURCE_ROOTS)]
        self.assertEqual(self.hook("stop", args=[self.dir, json.dumps(roots), "stop"]), "")
        self.assertEqual(self.records()[0]["payload"], {"callback_imported": True})

    def test_callback_namespace_cannot_hide_a_package_outside_source_roots(self):
        _, imported = self.callback_package()
        source = os.path.join(self.root, "source")
        os.makedirs(os.path.join(source, "callback_package"))
        script = """import runpy, sys
path, *arguments = sys.argv[1:]
sys.argv = [path, *arguments]
try:
    runpy.run_path(path, run_name="__main__")
except SystemExit as error:
    assert error.code == 0
assert "callback_package" not in sys.modules
"""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                HOOK,
                self.dir,
                json.dumps([source, *json.loads(SOURCE_ROOTS)]),
                "stop",
            ],
            input=b"{}",
            capture_output=True,
            env={**self.env, "PYTHONPATH": self.root},
            timeout=60,
            check=True,
        )
        self.assertEqual(result.stdout, b"")
        self.assertError(result.stderr.decode(), "module outside source roots")
        self.assertFalse(os.path.exists(imported))
        self.assertNothingWritten()

    def test_bad_arguments_write_nothing(self):
        for args in (
            [],
            [self.dir],
            [self.dir, "stop"],
            [self.dir, SOURCE_ROOTS, "stop", "x"],
            ["s1", SOURCE_ROOTS, "stop"],
            [self.dir + "\n", SOURCE_ROOTS, "stop"],
            [self.dir, SOURCE_ROOTS, ""],
            [self.dir, SOURCE_ROOTS, "a b"],
            [self.dir, SOURCE_ROOTS, "x" * 65],
            [self.dir, SOURCE_ROOTS, "../x"],
        ):
            with self.subTest(args):
                self.assertError(self.hook(None, args=args), "usage")
        self.assertNothingWritten()

    def test_bad_source_roots_are_rejected_before_importing_the_callback(self):
        _, invalid = source_roots_cases(self.root)
        callback, imported = (
            os.path.join(self.root, "adapter.py"),
            os.path.join(self.root, "imported"),
        )
        with open(callback, "w") as file:
            file.write(f"open({imported!r}, 'w').close()\n")
        self.state["adapter_hook"] = descriptor("adapter")
        write_state(self.dir, self.state)
        cases = {
            "malformed JSON": "not-json",
            "excessive nesting": "[" * 1000 + "]" * 1000,
            **{name: json.dumps(roots) for name, roots in invalid.items()},
        }
        for name, encoded in cases.items():
            with self.subTest(roots=name):
                self.assertError(
                    self.hook("stop", args=[self.dir, encoded, "stop"]), "source roots"
                )
                self.assertFalse(os.path.exists(imported))
        self.assertNothingWritten()

    def test_valid_source_roots_reach_the_adapter_callback(self):
        valid, _ = source_roots_cases(self.root)
        callback = os.path.join(self.root, "adapter.py")
        with open(callback, "w") as file:
            file.write(
                "def on_hook(call):\n    return {**call.payload, 'callback_imported': True}\n"
            )
        self.state.update(adapter_hook=descriptor("adapter"), words={})
        write_state(self.dir, self.state)
        for name, roots in valid.items():
            with self.subTest(roots=name):
                roots = [self.root, *roots]
                self.assertEqual(self.hook("stop", args=[self.dir, json.dumps(roots), "stop"]), "")
                self.assertEqual(self.records()[-1]["payload"], {"callback_imported": True})

    def test_supplied_distribution_roots_lead_once_before_callback_imports(self):
        sources = [
            os.path.join(self.root, name) for name in ("first-distribution", "second-distribution")
        ]
        for source, value in zip(sources, ("first", "second"), strict=True):
            os.mkdir(source)
            with open(os.path.join(source, "source_marker.py"), "w") as file:
                file.write(f"VALUE = {value!r}\n")
        roots = [*sources, *json.loads(SOURCE_ROOTS)]
        callback = os.path.join(sources[0], "adapter.py")
        with open(callback, "w") as file:
            file.write(
                f"""import sys
from source_marker import VALUE
def on_hook(call):
    payload = dict(call.payload)
    payload["source"] = VALUE
    payload["roots"] = sys.path[:2]
    payload["counts"] = [sys.path.count(path) for path in sys.path[:2]]
    payload["import_roots"] = sys.path[:{len(roots)}]
    payload["tail"] = sys.path[{len(roots)}:]
    return payload
"""
            )
        self.state.update(adapter_hook=descriptor("adapter"), words={})
        write_state(self.dir, self.state)
        script = """import io, json, runpy, sys
path, folder, encoded = sys.argv[1:]
roots = json.loads(encoded)
sys.path[:0] = [*roots, *roots]
main = runpy.run_path(path)["main"]
for _ in range(2):
    main([folder, encoded, "stop"], io.BytesIO(b"{}"))
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, HOOK, self.dir, json.dumps(roots)],
            capture_output=True,
            env=self.env,
            check=True,
        )
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        records = self.records()
        self.assertEqual(len(records), 2)
        for record in records:
            payload = record["payload"]
            self.assertEqual(
                (payload["source"], payload["roots"], payload["counts"]), ("first", sources, [1, 1])
            )
            self.assertEqual(payload["import_roots"], roots)
            self.assertNotIn(SRC["session"], payload["tail"])
            self.assertNotIn(SRC["utils"], payload["tail"])

    def test_a_bad_session_dir_writes_nothing(self):
        afile = os.path.join(self.root, "file")
        open(afile, "w").close()
        for folder in (os.path.join(self.root, "missing"), afile):
            with self.subTest(folder):
                self.assertError(
                    self.hook(None, args=[folder, SOURCE_ROOTS, "stop"]), "session.json"
                )
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
            "workdir": ("", "rel", "/a\x00b", None),
        }
        self.assertEqual(set(bad), set(tui_hook.SESSION_STATE))
        for field, values in bad.items():
            for value in (*values, MISSING):
                with self.subTest(field=field, value=value):
                    changed = {k: v for k, v in self.state.items() if k != field}
                    if value is not MISSING:
                        changed[field] = value
                    write_state(self.dir, changed)
                    self.assertError(self.hook("stop"), "session.json")
                    self.assertNothingWritten()

    def test_identity_keys_neither_it_nor_its_adapter_hook_reads_go_unchecked(self):
        """SessionInfo.from_fields is their one decoder; the hook reads words and events,
        and a codex adapter_hook workdir and sid.
        """
        unread = {
            "runtime": ("Fake", None),
            "cwd": ("", "rel", None),
            "transcript": ("rel", None),
            "project": ("true", None),
            "resume": (None, 1),
        }
        for field, values in unread.items():
            for value in (*values, MISSING):
                with self.subTest(field=field, value=value):
                    changed = {k: v for k, v in self.state.items() if k != field}
                    if value is not MISSING:
                        changed[field] = value
                    write_state(self.dir, changed)
                    self.assertEqual(self.hook("other"), "")
        self.assertEqual(len(self.records()), sum(len(v) + 1 for v in unread.values()))

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


if __name__ == "__main__":
    unittest.main()
