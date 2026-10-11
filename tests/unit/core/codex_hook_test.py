"""The Codex worker hook helper (core/src/clients/_codex_hook.py), run as Codex runs it: a process, its payload on
stdin, $TMUX_PANE set; its tmux a fake that logs each call with the record and reply as they are then."""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid

from tests import ROOT

HOOK = os.path.join(ROOT, "core", "src", "clients", "_codex_hook.py")
PANE = "%3"
LINE = r"\A[0-9]{2}:[0-9]{2}:[0-9]{2} w1 %s\n\Z"   # one event line
TMUX = """#!{python}
import json, os, sys
files = {{}}
for path in os.environ["WATCH"].split(os.pathsep):
    try:
        with open(path) as f:
            files[os.path.basename(path)] = f.read()
    except OSError:
        files[os.path.basename(path)] = None
with open(os.environ["TMUX_LOG"], "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "files": files}}) + "\\n")
if sys.argv[1] == "display-message":
    print("w1")
"""


class HookTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.cwd = os.path.join(self.dir, "project")
        self.workers = os.path.join(self.dir, "workers")
        os.mkdir(self.cwd)
        os.mkdir(self.workers, 0o700)
        self.sid = str(uuid.uuid4())
        self.native = str(uuid.uuid4())
        self.record = os.path.join(self.workers, f"{self.sid}.json")
        self.reply = os.path.join(self.workers, f"{self.sid}.reply")
        self.events = os.path.join(self.dir, "events")
        os.close(os.open(self.events, os.O_WRONLY | os.O_CREAT, 0o600))
        self.tmux = os.path.join(self.dir, "tmux")
        with open(self.tmux, "w") as f:
            f.write(TMUX.format(python=sys.executable))
        os.chmod(self.tmux, 0o755)
        self.log = os.path.join(self.dir, "tmux.log")
        self.write_record(None)

    def write_record(self, native, **fields):
        fd = os.open(self.record, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "sid": self.sid, "cwd": self.cwd, "codex_home": os.path.join(self.dir, "codex"),
                       "native": native, **fields}, f)

    def write_reply(self, text):
        fd = os.open(self.reply, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)

    def hook(self, word, payload, args=None, events=None):
        """Runs the helper; checks it exits 0 printing nothing on stdout; returns its stderr."""
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        args = [word, events or self.events, self.record, self.tmux] if args is None else args
        env = {"PATH": "/usr/bin:/bin", "TMUX_PANE": PANE, "TMUX_LOG": self.log,
               "WATCH": os.pathsep.join((self.record, self.reply))}
        res = subprocess.run([sys.executable, "-I", HOOK, *args], input=data, capture_output=True, env=env,
                             timeout=60)
        self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)
        return res.stderr.decode()

    def payload(self, event, **extra):
        return {"session_id": self.native, "transcript_path": None, "cwd": self.cwd, "hook_event_name": event,
                "model": "m", "permission_mode": "default", "turn_id": "t1", **extra}

    def stop(self, message="the answer"):
        return self.payload("Stop", stop_hook_active=False, last_assistant_message=message)

    def bound(self):
        with open(self.record) as f:
            return json.load(f)["native"]

    def replied(self):
        try:
            with open(self.reply) as f:
                return f.read()
        except FileNotFoundError:
            return None

    def lines(self):
        with open(self.events) as f:
            return f.read()

    def calls(self):
        try:
            with open(self.log) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def marked(self, state):
        """The tmux calls tmux_host.mark makes for state."""
        calls = [["set-option", "-t", PANE, "@state", state]]
        return calls + [["display-message", "-p", "-t", PANE, "#S"]] if state in ("done", "blocked") else calls

    def test_stop_binds_then_saves_the_reply_then_marks_done(self):
        message = "**Done**: 完成\n\n- one\n- two"
        self.assertEqual(self.hook("done", self.stop(message)), "")
        self.assertEqual(self.bound(), self.native)
        with open(self.record) as f:
            self.assertEqual(json.load(f), {"version": 1, "sid": self.sid, "cwd": self.cwd,
                                            "codex_home": os.path.join(self.dir, "codex"), "native": self.native})
        self.assertEqual(self.replied(), message)
        for path in (self.record, self.reply):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(self.workers)), sorted((f"{self.sid}.json", f"{self.sid}.reply")))
        calls = self.calls()
        self.assertEqual([c["argv"] for c in calls], self.marked("done"))
        self.assertEqual(json.loads(calls[0]["files"][f"{self.sid}.json"])["native"], self.native)
        self.assertEqual(calls[0]["files"][f"{self.sid}.reply"], message)
        self.assertRegex(self.lines(), LINE % "done")

    def test_each_word_binds_and_marks_its_state(self):
        """session and working write no event line; interrupt is done."""
        for word, event, state in (("session", "SessionStart", "working"), ("working", "UserPromptSubmit", "working"),
                                   ("blocked", "PermissionRequest", "blocked"), ("interrupt", "Interrupt", "done")):
            with self.subTest(word=word):
                self.write_record(None)
                open(self.events, "w").close()
                if os.path.exists(self.log):
                    os.unlink(self.log)
                self.assertEqual(self.hook(word, self.payload(event)), "")
                self.assertEqual(self.bound(), self.native)
                self.assertEqual([c["argv"] for c in self.calls()], self.marked(state))
                self.assertRegex(self.lines(), r"\A\Z" if state == "working" else LINE % state)
                self.assertIsNone(self.replied())

    def test_an_interrupt_or_a_null_message_keeps_the_reply(self):
        self.write_record(self.native)
        self.write_reply("old")
        for word, payload in (("interrupt", self.payload("Interrupt")), ("done", self.stop(None)),
                              ("done", self.payload("Stop", stop_hook_active=False))):
            with self.subTest(word=word, payload=payload):
                open(self.events, "w").close()
                self.assertEqual(self.hook(word, payload), "")
                self.assertEqual(self.replied(), "old")
                self.assertRegex(self.lines(), LINE % "done")

    def test_a_bound_record_is_kept_as_it_is(self):
        self.write_record(self.native)
        with open(self.record, "rb") as f:
            before = f.read()
        self.assertEqual(self.hook("done", self.stop("again")), "")
        with open(self.record, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.replied(), "again")

    def test_another_session_neither_rebinds_nor_replies(self):
        self.write_record(self.native)
        self.write_reply("old")
        other = str(uuid.uuid4())
        err = self.hook("done", {**self.stop("theirs"), "session_id": other})
        self.assertEqual(err, f"codex_hook: {self.record}: bound to {self.native}, not {other}\n")
        self.assertEqual((self.bound(), self.replied()), (self.native, "old"))
        self.assertRegex(self.lines(), LINE % "done")

    def test_a_sub_agent_another_cwd_or_a_bad_id_neither_binds_nor_replies(self):
        elsewhere = os.path.join(self.dir, "elsewhere")
        os.mkdir(elsewhere)
        for change in ({"agent_id": "a1", "agent_type": "worker"}, {"cwd": elsewhere}, {"cwd": None},
                       {"session_id": self.native.upper()}, {"session_id": "not-a-uuid"}, {"session_id": None}):
            with self.subTest(change=change):
                open(self.events, "w").close()
                self.assertEqual(self.hook("done", {**self.stop(), **change}), "")
                self.assertEqual((self.bound(), self.replied()), (None, None))
                self.assertRegex(self.lines(), LINE % "done")

    def test_a_null_agent_id_and_a_symlinked_cwd_bind(self):
        link = os.path.join(self.dir, "link")
        os.symlink(self.cwd, link)
        self.assertEqual(self.hook("done", {**self.stop(), "agent_id": None, "cwd": link}), "")
        self.assertEqual((self.bound(), self.replied()), (self.native, "the answer"))

    def test_done_is_marked_when_the_record_fails(self):
        """A missing, symlinked, malformed or foreign record: a stderr line, nothing bound or replied, done marked."""
        target = os.path.join(self.dir, "target.json")
        os.rename(self.record, target)
        os.chmod(target, 0o600)

        def symlink():
            os.symlink(target, self.record)

        def malformed(**fields):
            return lambda: self.write_record(None, **fields)

        def mode():
            self.write_record(None)
            os.chmod(self.record, 0o644)
        for name, setup in (("missing", lambda: None), ("symlink", symlink), ("extra key", malformed(extra=1)),
                            ("other sid", malformed(sid=str(uuid.uuid4()))), ("version", malformed(version=2)),
                            ("mode", mode)):
            with self.subTest(name):
                if os.path.lexists(self.record):
                    os.unlink(self.record)
                setup()
                open(self.events, "w").close()
                err = self.hook("done", self.stop())
                self.assertRegex(err, rf"\Acodex_hook: {re.escape(self.record)}: .+\n\Z")
                self.assertIsNone(self.replied())
                self.assertRegex(self.lines(), LINE % "done")
        with open(target) as f:
            self.assertIsNone(json.load(f)["native"])

    def test_a_mark_failure_is_a_stderr_line(self):
        link = os.path.join(self.dir, "events-link")
        os.symlink(self.events, link)
        err = self.hook("done", self.stop(), events=link)
        self.assertRegex(err, r"\Acodex_hook: events file .+\n\Z")
        self.assertEqual((self.bound(), self.replied(), self.lines()), (self.native, "the answer", ""))

    def test_bad_arguments_do_nothing(self):
        for args in ([], ["done"], ["done", self.events, self.record], ["bogus", self.events, self.record, self.tmux],
                     ["done", self.events, self.record, "tmux"], ["done", self.events, self.reply, self.tmux],
                     ["done", "events", self.record, self.tmux], ["done", self.events, self.record, self.tmux, "x"]):
            with self.subTest(args=args):
                self.assertRegex(self.hook("done", self.stop(), args=args), r"\Acodex_hook: usage: .+\n\Z")
                self.assertEqual((self.bound(), self.replied(), self.lines(), self.calls()), (None, None, "", []))

    def test_a_bad_payload_binds_nothing_and_still_marks(self):
        for data in (b"", b"not json", b"[1]", b'"text"', b"\xff\xfe\x00"):
            with self.subTest(data=data):
                open(self.events, "w").close()
                self.assertEqual(self.hook("done", data), "")
                self.assertEqual((self.bound(), self.replied()), (None, None))
                self.assertRegex(self.lines(), LINE % "done")

    def test_the_payload_is_read_to_its_end(self):
        """One past the pipe's buffer is parsed; one over 64 MiB is read, not parsed."""
        message = "x" * (1 << 20)
        self.assertEqual(self.hook("done", self.stop(message)), "")
        self.assertEqual(self.replied(), message)
        self.write_record(None)
        os.unlink(self.reply)
        open(self.events, "w").close()
        big = json.dumps(self.stop("y" * (64 << 20))).encode()
        self.assertEqual(self.hook("done", big), "")
        self.assertEqual((self.bound(), self.replied()), (None, None))
        self.assertRegex(self.lines(), LINE % "done")


if __name__ == "__main__":
    unittest.main()
