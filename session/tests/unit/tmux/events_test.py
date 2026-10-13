"""Tmux events behavior at standard-library boundaries."""

import os
import re
import stat
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import ExitStack

from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.errors.types import TuiError

from session.tests.case import Case
from session.tests.unit.tmux.support import (
    ABSENT,
    FAILED,
    FORMAT,
    MARK_KW,
    TMUX,
    TMUX_KW,
    Tmux,
    environ,
)


class MarkTest(Case):
    """The event protocol a hook writes: the pane's @state, and done or blocked lines in the events
    file.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.events = os.path.join(self.root, "events")
        with open(self.events, "w") as f:
            f.write("old\n")
        self.enterContext(environ(TMUX_PANE="%1"))

    def text(self):
        with open(self.events) as f:
            return f.read()

    def test_working_sets_the_state_only(self):
        fake = Tmux(pane="w1")
        tmux_host.mark("working", self.events, proc=fake)
        self.assertEqual(fake.calls, [["tmux", "set-option", "-t", "%1", "@state", "working"]])
        self.assertEqual(fake.kwargs, [MARK_KW])
        self.assertEqual(self.text(), "old\n")

    def test_done_and_blocked_set_the_state_and_append_a_line_through_the_given_tmux(self):
        for word in ("done", "blocked"):
            with self.subTest(word):
                fake = Tmux(pane="w1")
                tmux_host.mark(word, self.events, pane="%2", tmux=TMUX, proc=fake)
                self.assertEqual(
                    fake.calls,
                    [
                        [TMUX, "set-option", "-t", "%2", "@state", word],
                        [TMUX, "display-message", "-p", "-t", "%2", "#S"],
                    ],
                )
                self.assertEqual(fake.kwargs, [MARK_KW] * 2)
        self.assertRegex(self.text(), r"^old\n\d\d:\d\d:\d\d w1 done\n\d\d:\d\d:\d\d w1 blocked\n$")

    def test_no_events_file_sets_the_state_only(self):
        for word in ("done", "blocked"):
            with self.subTest(word):
                fake = Tmux(pane="w1")
                tmux_host.mark(word, None, proc=fake)
                self.assertEqual(fake.calls, [["tmux", "set-option", "-t", "%1", "@state", word]])
        self.assertEqual(self.text(), "old\n")

    def test_its_pane_is_tmux_pane_else_nothing_is_marked(self):
        fake = Tmux(pane="w1")
        tmux_host.mark("done", self.events, proc=fake)
        self.assertEqual([c[c.index("-t") + 1] for c in fake.calls], ["%1", "%1"])
        for env in ({}, {"TMUX_PANE": ""}):
            with self.subTest(env=env), environ(**env):
                fake = Tmux(pane="w1")
                tmux_host.mark("done", self.events, proc=fake)
                self.assertEqual(fake.calls, [])
        self.assertEqual(len(self.text().splitlines()), 2)

    def test_refuses_an_events_file_not_a_regular_file_of_the_callers(self):
        link = os.path.join(self.root, "link")
        os.symlink(self.events, link)
        fifo = unittest.mock.Mock(st_mode=stat.S_IFIFO | 0o600, st_uid=os.getuid())
        for path, patches, error in (
            (link, (), ""),
            (self.root, (), ""),
            (os.path.join(self.root, "missing"), (), "No such file"),
            (self.events, ("os.getuid", os.getuid() + 1), "owned by you"),
            (self.events, ("os.fstat", fifo), "owned by you"),
        ):
            with (
                self.subTest(path=path, error=error),
                ExitStack() as stack,
                self.assertRaisesRegex(
                    tmux_host.TuiError, f"^events file {re.escape(path)}: .*{error}"
                ),
            ):
                if patches:
                    stack.enter_context(unittest.mock.patch(patches[0], return_value=patches[1]))
                tmux_host.mark("done", path, proc=Tmux(pane="w1"))
        self.assertEqual(self.text(), "old\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "missing")))

    def test_a_failed_tmux_call_or_bad_value_raises_writing_no_line(self):
        timeout = subprocess.TimeoutExpired(["tmux"], 5)
        cases = (
            (Tmux(pane="w1", fail={"set-option"}), {}, "^tmux: boom$"),
            (Tmux(), {}, "^tmux: no server running$"),
            (Tmux(results={"set-option": timeout}), {}, "^tmux: timed out after 5 s$"),
            (
                Tmux(results={"display-message": FileNotFoundError(2, "No such file")}),
                {},
                "^tmux: ",
            ),
            (Tmux(pane="a b"), {}, "^tmux session 'a b'"),
            (Tmux(pane="w1"), {"pane": "w1:0"}, "^bad tmux pane 'w1:0'$"),
        )
        for fake, kw, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(tmux_host.TuiError, error):
                tmux_host.mark("done", self.events, proc=fake, **kw)
        self.assertEqual(self.text(), "old\n")


class EventsFileTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)

    def test_created_0600_and_made_absolute(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        path = os.path.join(self.root, "e")
        self.assertEqual(tmux_host.events_file("e"), path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, "a") as f:
            f.write("x\n")
        self.assertEqual(tmux_host.events_file(path), path)
        with open(path) as f:
            self.assertEqual(f.read(), "x\n")

    def test_refused(self):
        target = os.path.join(self.root, "target")
        open(target, "w").close()
        link = os.path.join(self.root, "link")
        os.symlink(target, link)
        for path in (link, self.root, os.path.join(self.root, "missing", "e")):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(tmux_host.TuiError, f"^events file {re.escape(path)}: "),
            ):
                tmux_host.events_file(path)
        fifo = unittest.mock.Mock(st_mode=stat.S_IFIFO | 0o600, st_uid=os.getuid())
        for patch in (
            unittest.mock.patch("os.getuid", return_value=os.getuid() + 1),
            unittest.mock.patch("os.fstat", return_value=fifo),
        ):
            with (
                self.subTest(patch=patch),
                patch,
                self.assertRaisesRegex(tmux_host.TuiError, "owned by you"),
            ):
                tmux_host.events_file(target)

    def test_a_fifo_without_a_reader_is_refused_at_once(self):
        fifo = os.path.join(self.root, "fifo")
        os.mkfifo(fifo, 0o600)
        caught = []

        def run():
            try:
                tmux_host.events_file(fifo)
            except TuiError as e:
                caught.append(e)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(5)
        if thread.is_alive():
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # lets the blocked open return
            thread.join()
            os.close(reader)
            self.fail("events_file blocked on a FIFO without a reader")
        self.assertRegex(str(caught[0]), f"^events file {re.escape(fifo)}: ")

    def test_refuses_a_path_tmux_would_misread(self):
        for name in ("a#b", "e;"):
            path = os.path.join(self.root, name)
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    tmux_host.TuiError, f"^events file {re.escape(path)}: tmux would misread"
                ),
            ):
                tmux_host.events_file(path)
            self.assertFalse(os.path.exists(path))


class StatusTest(Case):
    def test_states(self):
        # a signal death: -n (tmux prints the signal's name where libc has one, else its number)
        for pane, want in (
            ("0  ", tmux_host.RUNNING),
            ("1 3 ", 3),
            ("1 0 ", 0),
            ("1  kill", -9),
            ("1  9", -9),
            ("1  term", -15),
            ("1  ", tmux_host.RUNNING),
            ("  ", None),
            (None, None),
        ):
            with self.subTest(pane=pane):
                fake = Tmux(pane=pane)
                self.assertEqual(tmux_host.status("s", proc=fake), want)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_absent_for_a_missing_session_or_server_else_unknown(self):
        self.assertEqual(tmux_host.UNKNOWN, "unknown")
        missing = FileNotFoundError(2, "No such file or directory", "tmux")
        for result, want in (
            *(((1, "", err + "\n"), None) for err in ABSENT),
            *(((1, "", err + "\n"), tmux_host.UNKNOWN) for err in FAILED),
            ((0, "x\n"), tmux_host.UNKNOWN),
            ((0, "2 0 \n"), tmux_host.UNKNOWN),
            ((0, "1 0\n"), tmux_host.UNKNOWN),
            ((0, ""), tmux_host.UNKNOWN),
            (missing, tmux_host.UNKNOWN),
            (PermissionError(13, "Permission denied"), tmux_host.UNKNOWN),
        ):
            with self.subTest(result=result):
                fake = Tmux(results={"display-message": result})
                self.assertEqual(tmux_host.status("s", proc=fake), want)
                self.assertEqual(len(fake.calls), 1)


class TmuxPathTest(Case):
    def test_the_real_path_of_the_tmux_on_path_else_none(self):
        with tempfile.TemporaryDirectory() as d:
            real, bin_dir = os.path.join(d, "real"), os.path.join(d, "bin")
            os.mkdir(real)
            os.mkdir(bin_dir)
            exe = os.path.join(real, "tmux-3.7")
            with open(exe, "w"):
                pass
            os.chmod(exe, 0o755)
            os.symlink(exe, os.path.join(bin_dir, "tmux"))
            with environ(PATH=bin_dir):
                self.assertEqual(
                    tmux_host.tmux_path(), os.path.join(os.path.realpath(d), "real", "tmux-3.7")
                )
            for path in (real, ""):
                with self.subTest(path=path), environ(PATH=path):
                    self.assertIsNone(tmux_host.tmux_path())


class KillTest(Case):
    def test_kill(self):
        fake = Tmux()
        tmux_host.kill("s", proc=fake)
        self.assertEqual(fake.calls, [["tmux", "kill-session", "-t", "=s"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_failure(self):
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            tmux_host.kill("s", proc=Tmux(fail={"kill-session"}))
