"""Host callbacks bind their saved server and launch generation before marking."""

import os
import re
import stat
import tempfile
import unittest
import unittest.mock
from contextlib import ExitStack
from unittest import mock

from agent_pm.session.engine.types import HookCall
from agent_pm.session.hosts.tmux import hook, types

from session.tests.case import Case
from session.tests.tmux_fixtures import (
    MARK_KW,
    TMUX,
    FakeTmux,
    environ,
)
from utils.tests.process_fixtures import TimeoutExpired


class CallbackTest(Case):
    def setUp(self):
        self.executable = os.path.join(self.home, "executable")
        with open(self.executable, "w"):
            pass
        os.chmod(self.executable, 0o700)
        self.state = {
            "executable": self.executable,
            "target": "w1",
            "generation": "generation",
            "environment": {"TMUX_TMPDIR": self.home},
        }

    def run_callback(self, identity, process="4242 $3 %3 4343"):
        response = "" if identity is None else f"{process}\t{identity}\n"
        with (
            mock.patch.dict(
                os.environ,
                {"TMUX": "foreign", "TMUX_TMPDIR": "/foreign", "KEEP": "value"},
                clear=True,
            ),
            mock.patch(
                "subprocess.run",
                side_effect=[
                    mock.Mock(returncode=0, stdout=response, stderr=""),
                    mock.Mock(returncode=0, stdout="", stderr=""),
                ],
            ) as run,
        ):
            hook.on_hook("working", HookCall("tool", {}, "sid", self.home, None, self.state))
        return run

    def test_callback_uses_the_saved_selector_for_lookup_and_mark(self):
        run = self.run_callback("sid\tgeneration")
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                [
                    self.executable,
                    "display-message",
                    "-p",
                    "-t",
                    "=w1:",
                    "#{@process}\t#{@sid}\t#{@generation}",
                ],
                [self.executable, "set-option", "-t", "%3", "@state", "working"],
            ],
        )
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"], {"TMUX_TMPDIR": self.home, "KEEP": "value"})

    def test_callback_marks_the_pane_its_launch_recorded(self):
        run = self.run_callback("sid\tgeneration", process="1 $1 %9 42")
        self.assertEqual(
            run.call_args_list[1].args[0],
            [self.executable, "set-option", "-t", "%9", "@state", "working"],
        )

    def test_a_missing_or_malformed_process_marks_nothing(self):
        for process in ("", "%9", "1 $1 %9", "1 $1 %9 42;"):
            with self.subTest(process=process):
                self.assertEqual(
                    self.run_callback("sid\tgeneration", process=process).call_count, 1
                )

    def test_a_reused_name_even_with_the_same_sid_is_never_marked(self):
        for identity in ("sid\tnext-generation", "other\tgeneration", "", None):
            with self.subTest(identity=identity):
                self.assertEqual(self.run_callback(identity).call_count, 1)


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
        fake = FakeTmux(pane="w1")
        hook.mark("working", self.events, proc=fake)
        self.assertEqual(fake.calls, [["tmux", "set-option", "-t", "%1", "@state", "working"]])
        self.assertEqual(fake.kwargs, [MARK_KW])
        self.assertEqual(self.text(), "old\n")

    def test_done_and_blocked_set_the_state_and_append_a_line_through_the_given_tmux(self):
        for word in ("done", "blocked"):
            with self.subTest(word):
                fake = FakeTmux(pane="w1")
                hook.mark(word, self.events, pane="%2", tmux=TMUX, proc=fake)
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
                fake = FakeTmux(pane="w1")
                hook.mark(word, None, proc=fake)
                self.assertEqual(fake.calls, [["tmux", "set-option", "-t", "%1", "@state", word]])
        self.assertEqual(self.text(), "old\n")

    def test_its_pane_is_tmux_pane_else_nothing_is_marked(self):
        fake = FakeTmux(pane="w1")
        hook.mark("done", self.events, proc=fake)
        self.assertEqual([c[c.index("-t") + 1] for c in fake.calls], ["%1", "%1"])
        for env in ({}, {"TMUX_PANE": ""}):
            with self.subTest(env=env), environ(**env):
                fake = FakeTmux(pane="w1")
                hook.mark("done", self.events, proc=fake)
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
                    types.TuiError, f"^events file {re.escape(path)}: .*{error}"
                ),
            ):
                if patches:
                    stack.enter_context(unittest.mock.patch(patches[0], return_value=patches[1]))
                hook.mark("done", path, proc=FakeTmux(pane="w1"))
        self.assertEqual(self.text(), "old\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "missing")))

    def test_a_failed_tmux_call_or_bad_value_raises_writing_no_line(self):
        timeout = TimeoutExpired(["tmux"], 5)
        cases = (
            (FakeTmux(pane="w1", fail={"set-option"}), {}, "^tmux: boom$"),
            (FakeTmux(), {}, "^tmux: no server running$"),
            (FakeTmux(results={"set-option": timeout}), {}, "^tmux: timed out after 5 s$"),
            (
                FakeTmux(results={"display-message": FileNotFoundError(2, "No such file")}),
                {},
                "^tmux: ",
            ),
            (FakeTmux(pane="a b"), {}, "^tmux session 'a b'"),
            (FakeTmux(pane="w1"), {"pane": "w1:0"}, "^bad tmux pane 'w1:0'$"),
        )
        for fake, kw, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(types.TuiError, error):
                hook.mark("done", self.events, proc=fake, **kw)
        self.assertEqual(self.text(), "old\n")
