"""Launch handoff, events and terminal transport through recorded commands."""

import errno
import os
import re
import signal
import stat
import sys
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import ExitStack

from agent_pm.session.engine.types import ConfigError, Launch
from agent_pm.session.hosts.tmux import tmux_host, types
from agent_pm.session.hosts.tmux.tmux_host import TuiError

from session.tests.case import Case
from session.tests.tmux_fixtures import (
    ABSENT,
    EXITED,
    FAILED,
    FORMAT,
    INSIDE,
    ITERM,
    MARK_KW,
    NEW,
    NEW_PROCESS,
    PREFIXES,
    SIGNALS,
    TMUX,
    TMUX_KW,
    StartFixture,
    Tmux,
    chained,
    decorations,
    environ,
    which,
)
from utils.tests.process_fixtures import TimeoutExpired


class DetachTest(StartFixture):
    """Session d hosting argv as old drive.detach did: no decoration, no dead pane kept."""

    def detach(self, fake, session="d", argv=("tool", "-x", "a b"), env=None, **kw):
        env = {"PATH": self.bin, "HOME": "/h"} if env is None else env
        return tmux_host.detach(
            session, Launch(tuple(argv), env, cwd=self.cwd), **kw, proc=fake, sleep=self.sleep
        )

    def test_new_session_runs_the_wrapper_and_returns_its_process_once_argv_execd(self):
        fake = Tmux()
        self.assertEqual(self.detach(fake), NEW_PROCESS)
        self.assertEqual(
            fake.calls,
            [
                [
                    "tmux",
                    "new-session",
                    "-d",
                    *NEW,
                    "-s",
                    "d",
                    sys.executable,
                    "-I",
                    "-c",
                    tmux_host.EXEC,
                    fake.path,
                    fake.fifo,
                    ";",
                    "set-option",
                    "-t",
                    "=d:",
                    "@kind",
                    "detach",
                ]
            ],
        )
        self.assertEqual(fake.kwargs, [TMUX_KW])
        self.assertNotIn("remain-on-exit", fake.calls[0])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_with_the_blocked_signals(self):
        env = {
            "PATH": self.bin,
            "HOME": "/h",
            "CHILD_SESSION": "1",
            "PWD": "/elsewhere",
            **{k: "x" for k in tmux_host.TERMINAL_KEYS},
        }
        for block, want in (((), []), ((signal.SIGHUP, signal.SIGTERM, signal.SIGINT), SIGNALS)):
            with self.subTest(block=block):
                fake = Tmux()
                self.detach(fake, env=env, block=block)
                self.assertEqual(
                    fake._handover,
                    {
                        "argv": [self.tool, "-x", "a b"],
                        "cwd": self.cwd,
                        "env": {
                            "PATH": self.bin,
                            "HOME": "/h",
                            "CHILD_SESSION": "1",
                            "PWD": self.cwd,
                        },
                        "block": want,
                    },
                )
                self.assertEqual(fake.mode, 0o600)
                self.assertEqual(fake.dir_mode, 0o700)
                self.assertEqual(os.listdir(self.temp), [])

    def test_the_envs_iterm_session_id_goes_to_the_session_only_in_its_iterm2_form(self):
        for value, given in (
            ("w0t0p0:ABC", True),
            ("w0t1p0:6D7E-8F", True),
            (None, False),
            ("", False),
            ("w0t0p0", False),
            ("cmd", False),
            ("w0t0p0:a;b", False),
            ("w0t0p0:ABC\n", False),
            (":ABC", False),
            ("w0t0p0:A_B", False),
        ):
            with self.subTest(value=value):
                fake = Tmux()
                env = {"PATH": self.bin, **({} if value is None else {"ITERM_SESSION_ID": value})}
                self.detach(fake, env=env)
                head = [
                    "tmux",
                    "new-session",
                    "-d",
                    *NEW,
                    *(["-e", f"ITERM_SESSION_ID={value}"] if given else []),
                    "-s",
                    "d",
                ]
                self.assertEqual(fake.calls[0][: len(head) + 1], [*head, sys.executable])
                self.assertNotIn("ITERM_SESSION_ID", fake._handover["env"])

    def test_opener_set_in_the_same_call(self):
        for opener in ("mgr", "w0t0p0:ABC"):
            with self.subTest(opener=opener):
                fake = Tmux()
                self.detach(fake, opener=opener)
                self.assertEqual(
                    fake.calls,
                    [
                        [
                            "tmux",
                            "new-session",
                            "-d",
                            *NEW,
                            "-s",
                            "d",
                            sys.executable,
                            "-I",
                            "-c",
                            tmux_host.EXEC,
                            fake.path,
                            fake.fifo,
                            ";",
                            "set-option",
                            "-t",
                            "=d:",
                            "@kind",
                            "detach",
                            ";",
                            "set-option",
                            "-t",
                            "=d:",
                            "@opener",
                            opener,
                        ]
                    ],
                )

    def test_events_keep_the_pane_for_the_exited_hook_and_tags_set_in_the_same_call(self):
        events, line = os.path.join(self.root, "events"), '{"v":1,"name":"d"}'
        fake = Tmux()
        self.detach(fake, opener="mgr", display="mgr/right", events=events, record=line)
        self.assertEqual(
            fake.calls[0][fake.calls[0].index(fake.fifo) + 1 :],
            [
                ";",
                "set-option",
                "-p",
                "-t",
                "=d:",
                "remain-on-exit",
                "on",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@events",
                events,
                ";",
                "set-hook",
                "-p",
                "-t",
                "=d:",
                "pane-died",
                EXITED,
                ";",
                "set-option",
                "-t",
                "=d:",
                "@kind",
                "detach",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@opener",
                "mgr",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@display",
                "mgr/right",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@record",
                line,
            ],
        )
        self.assertEqual(stat.S_IMODE(os.stat(events).st_mode), 0o600)

    def test_a_display_or_record_is_stored_without_events_and_no_pane_kept(self):
        fake = Tmux()
        self.detach(fake, display="elsewhere:1.2", record="{}")
        self.assertEqual(
            fake.calls[0][fake.calls[0].index(fake.fifo) + 1 :],
            [
                ";",
                "set-option",
                "-t",
                "=d:",
                "@kind",
                "detach",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@display",
                "elsewhere:1.2",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@record",
                "{}",
            ],
        )

    def test_the_exited_hook_is_a_constant_reading_its_values_only_through_tmux_formats(self):
        self.assertEqual(tmux_host.EXITED_EVENT, EXITED)
        self.assertEqual(
            set(re.findall(r"#\{[^{}]*\}", EXITED)),
            {
                "#{q:session_name}",
                "#{pane_dead_signal}",
                "#{q:pane_dead_signal}",
                "#{pane_dead_status}",
                "#{q:@events}",
            },
        )
        self.assertEqual(EXITED.count("{"), EXITED.count("#{"))
        self.assertEqual(EXITED.count("{"), EXITED.count("}"))
        hooks = []
        for session, events in (("d", "e1"), ("other-name", "e 2 'x'")):
            fake = Tmux()
            self.detach(fake, session=session, events=os.path.join(self.root, events))
            hooks.append(fake.calls[0][fake.calls[0].index("pane-died") + 1])
        self.assertEqual(hooks, [EXITED, EXITED])

    def test_a_bad_display_record_or_events_is_refused_before_tmux(self):
        target, link = os.path.join(self.root, "target"), os.path.join(self.root, "link")
        open(target, "w").close()
        os.symlink(target, link)
        for kw, error in (
            ({"display": "mgr\tx"}, "^display 'mgr\\\\tx': want printable text not ending in ;$"),
            ({"display": "mgr\nx"}, "^display "),
            ({"display": "mgr;"}, "^display "),
            ({"display": 5}, "^display 5: "),
            ({"record": "{};"}, "^tag '@record'"),
            ({"record": "a\0b"}, "^tag '@record'"),
            ({"events": link}, f"^events file {re.escape(link)}: "),
            ({"events": os.path.join(self.root, "a#b")}, "tmux would misread"),
        ):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaisesRegex(types.TuiError, error):
                    self.detach(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_a_new_session_tmux_does_not_name_is_killed(self):
        for out in ("", "4242 $3 %7\n", "4242 $3 %7 x\n"):
            with self.subTest(out=out):
                fake = Tmux(results={"new-session": (0, out, "")})
                with self.assertRaisesRegex(types.TuiError, "^tmux: bad new session "):
                    self.detach(fake)
                self.assertEqual(fake.commands(), ["new-session", "kill-session"])
                self.assertEqual(os.listdir(self.temp), [])

    def test_an_existing_session_is_refused_untouched(self):
        fake = Tmux(results={"new-session": (1, "", "duplicate session: d\n")})
        with self.assertRaisesRegex(types.TuiError, r"^session d exists$"):
            self.detach(fake, opener="mgr")
        self.assertEqual(fake.commands(), ["new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_not_taken_kills_it(self):
        for fail in ((), {"kill-session"}):
            with self.subTest(fail=fail):
                self.sleep.reset_mock()
                fake = Tmux(fail=fail, hand_over=False)
                with self.assertRaisesRegex(types.TuiError, r"^session d did not start$"):
                    self.detach(fake)
                self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
                self.assertIs(fake.dir_at_kill, expr2=False)
                self.assertAlmostEqual(
                    sum(c.args[0] for c in self.sleep.call_args_list), tmux_host.HANDOVER_TIMEOUT
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_an_exec_failure_raises_its_error_and_kills_the_session(self):
        fake = Tmux(wrapper=errno.ENOENT)
        with self.assertRaisesRegex(
            types.TuiError,
            "^" + re.escape(f"cannot run {self.tool}: {os.strerror(errno.ENOENT)}") + "$",
        ):
            self.detach(fake)
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_never_execd_kills_it(self):
        fake = Tmux(wrapper="hang")
        with self.assertRaisesRegex(types.TuiError, r"^session d did not start$"):
            self.detach(fake)
        os.close(fake.writer)
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.assertAlmostEqual(
            sum(c.args[0] for c in self.sleep.call_args_list), tmux_host.HANDOVER_TIMEOUT
        )
        self.assertEqual(os.listdir(self.temp), [])

    def test_a_failed_tmux_call_kills_the_session_it_may_have_made(self):
        fake = Tmux(
            fail={"new-session"}
        )  # e.g. the chained set-option failed after new-session made the session
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            self.detach(fake, opener="mgr")
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.assertEqual(os.listdir(self.temp), [])

    def test_an_interrupt_after_new_session_kills_the_session(self):
        def interrupt(*_args, **_kw):
            raise KeyboardInterrupt

        fake, self.sleep = Tmux(hand_over=False), interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.detach(fake)
        self.assertEqual(fake.commands(), ["new-session", "kill-session"])
        self.assertIs(fake.dir_at_kill, expr2=False)
        self.assertEqual(os.listdir(self.temp), [])

    def test_launch_rejects_nul_before_tmux(self):
        fake = Tmux()
        with self.assertRaisesRegex(ConfigError, "launch: argv must be strings without NUL"):
            self.detach(fake, argv=("tool", "a\0b"))
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_refused_before_tmux(self):
        for kw, error in (
            ({"argv": ()}, "^no command$"),
            ({"argv": ("no-such-tool",)}, "no-such-tool"),
            ({"argv": ("tool", "\ud800")}, "encoded"),
            ({"opener": "a b"}, "opener"),
            ({"opener": "a:b:c"}, "opener"),
            ({"opener": ""}, "opener"),
        ):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaisesRegex(types.TuiError, error):
                    self.detach(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])
        with (
            unittest.mock.patch.object(sys, "executable", "/opt/py#3/bin/python3"),
            self.assertRaisesRegex(types.TuiError, "tmux would misread"),
        ):
            self.detach(fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_unwritable(self):
        fake = Tmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", os.path.join(self.root, "missing")),
            self.assertRaisesRegex(types.TuiError, "^handover: "),
        ):
            self.detach(fake)
        self.assertEqual(fake.calls, [])

    def test_cwd_never_reaches_tmux(self):
        self.cwd = os.path.join(self.root, "#(x)#{y}")
        os.makedirs(self.cwd)
        fake = Tmux()
        self.detach(fake)
        self.assertEqual(fake._handover["cwd"], self.cwd)
        self.assertEqual([a for c in fake.calls for a in c if "#(x)" in a or "#{y}" in a], [])


class ExecTest(Case):
    def test_safe_for_tmux(self):
        self.assertNotIn("#", tmux_host.EXEC)
        self.assertFalse(tmux_host.EXEC.rstrip().endswith(";"))


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
                    types.TuiError, f"^events file {re.escape(path)}: .*{error}"
                ),
            ):
                if patches:
                    stack.enter_context(unittest.mock.patch(patches[0], return_value=patches[1]))
                tmux_host.mark("done", path, proc=Tmux(pane="w1"))
        self.assertEqual(self.text(), "old\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "missing")))

    def test_a_failed_tmux_call_or_bad_value_raises_writing_no_line(self):
        timeout = TimeoutExpired(["tmux"], 5)
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
            with self.subTest(error=error), self.assertRaisesRegex(types.TuiError, error):
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
                self.assertRaisesRegex(types.TuiError, f"^events file {re.escape(path)}: "),
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
                self.assertRaisesRegex(types.TuiError, "owned by you"),
            ):
                tmux_host.events_file(target)

    def test_a_fifo_without_a_reader_is_refused_at_once(self):
        fifo = os.path.join(self.root, "fifo")
        os.mkfifo(fifo, 0o600)
        caught = []

        def run():
            for check in (tmux_host.validate_events, tmux_host.events_file):
                try:
                    check(fifo)
                except TuiError as e:
                    caught.append(str(e))

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(5)
        if thread.is_alive():
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # lets the blocked open return
            thread.join()
            os.close(reader)
            self.fail("blocked on a FIFO without a reader")
        self.assertEqual(caught, [f"events file {fifo}: not a regular file owned by you"] * 2)

    def test_a_parent_that_is_a_file_is_refused_with_nothing_made(self):
        parent = os.path.join(self.root, "file")
        open(parent, "w").close()
        path = os.path.join(parent, "e")
        for check in (tmux_host.validate_events, tmux_host.events_file):
            with self.subTest(check=check.__name__):
                with self.assertRaises(TuiError) as caught:
                    check(path)
                self.assertEqual(str(caught.exception), f"events file {path}: Not a directory")
        self.assertEqual(os.listdir(self.root), ["file"])
        self.assertEqual(os.path.getsize(parent), 0)

    def test_refuses_a_path_tmux_would_misread(self):
        for name in ("a#b", "e;"):
            path = os.path.join(self.root, name)
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    types.TuiError, f"^events file {re.escape(path)}: tmux would misread"
                ),
            ):
                tmux_host.events_file(path)
            self.assertFalse(os.path.exists(path))


class DisplayErrorTest(Case):
    def test_none_or_printable_text_not_ending_in_a_semicolon(self):
        for value in (None, "", "mgr", "mgr/right/m1", "elsewhere:1.2", "a b#{x}", "\u00e9"):
            with self.subTest(value=value):
                self.assertIsNone(tmux_host.display_error(value))
        for value in ("a\tb", "a\nb", "a\0b", "a\u2028b", "mgr;", 5, b"x"):
            with self.subTest(value=value):
                self.assertEqual(
                    tmux_host.display_error(value),
                    f"display {value!r:.80}: want printable text not ending in ;",
                )


class ProcessIdentityTest(Case):
    FORMAT = "#{pid} #{session_id} #{pane_id} #{pane_pid}"

    def test_the_server_session_pane_and_process_ids_of_its_pane_else_none(self):
        for target, printed, want in (
            ("s", (0, "4242 $3 %7 4343\n"), "4242 $3 %7 4343"),
            ("%7", (0, "4242 $3 %7 4343\n"), "4242 $3 %7 4343"),
            ("s", (0, " \n"), None),
            ("%7", (0, "4242   \n"), None),
            ("%7", (0, "4242  %7 4343\n"), None),
            ("s", (1, "", "can't find session: =s\n"), None),
            ("%7", (1, "", "can't find pane: %7\n"), None),
            ("s", (1, "", "no server running on /tmp/tmux-501/default\n"), None),
        ):
            with self.subTest(target=target, printed=printed):
                fake = Tmux(results={"display-message": printed})
                self.assertEqual(tmux_host.process(target, proc=fake), want)
                at = "=s:" if target == "s" else target
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", at, self.FORMAT]]
                )

    def test_a_failure_bad_output_or_bad_target_raises(self):
        for printed, error in (
            ((1, "", "boom\n"), "^tmux: boom$"),
            ((0, "4242 $3 %7\n"), "^bad tmux process "),
            ((0, "4242  %7 bad\n"), "^bad tmux process "),
            ((0, "bad  %7 4343\n"), "^bad tmux process "),
        ):
            with self.subTest(printed=printed):
                fake = Tmux(results={"display-message": printed})
                with self.assertRaisesRegex(types.TuiError, error):
                    tmux_host.process("s", proc=fake)
        fake = Tmux()
        with self.assertRaisesRegex(types.TuiError, "invalid target"):
            tmux_host.process("a b", proc=fake)
        self.assertEqual(fake.calls, [])


class StatusTest(Case):
    def test_states(self):
        # a signal death: -n (tmux prints the signal's name where libc has one, else its number)
        for pane, want in (
            ("0  ", types.PaneStatus.RUNNING),
            ("1 3 ", 3),
            ("1 0 ", 0),
            ("1  kill", -9),
            ("1  9", -9),
            ("1  term", -15),
            ("1  ", types.PaneStatus.RUNNING),
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

    def test_a_pane_status_is_a_str_enum_member(self):
        self.assertEqual(list(types.PaneStatus), ["running", "unknown"])
        for name in ("RUNNING", "UNKNOWN"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(tmux_host, name))
        for result, want in (
            ((0, "0  \n"), types.PaneStatus.RUNNING),
            ((0, "x\n"), types.PaneStatus.UNKNOWN),
        ):
            with self.subTest(result=result):
                fake = Tmux(results={"display-message": result})
                self.assertIs(tmux_host.status("s", proc=fake), want)

    def test_absent_for_a_missing_session_or_server_else_unknown(self):
        missing = FileNotFoundError(2, "No such file or directory", "tmux")
        for result, want in (
            *(((1, "", err + "\n"), None) for err in ABSENT),
            *(((1, "", err + "\n"), types.PaneStatus.UNKNOWN) for err in FAILED),
            ((0, "x\n"), types.PaneStatus.UNKNOWN),
            ((0, "2 0 \n"), types.PaneStatus.UNKNOWN),
            ((0, "1 0\n"), types.PaneStatus.UNKNOWN),
            ((0, ""), types.PaneStatus.UNKNOWN),
            (missing, types.PaneStatus.UNKNOWN),
            (PermissionError(13, "Permission denied"), types.PaneStatus.UNKNOWN),
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
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            tmux_host.kill("s", proc=Tmux(fail={"kill-session"}))


class SendTest(Case):
    def send(self, fake, text):
        tmux_host.send(
            "s", text, proc=fake, sleep=lambda seconds: fake.calls.append(("sleep", seconds))
        )
        buffer = fake.calls[0][3]
        self.assertRegex(buffer, r"^tui-s-[0-9a-f]{32}$")
        return buffer

    def test_pastes_the_text_then_presses_enter(self):
        fake = Tmux()
        buffer = self.send(fake, "-x hi")
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "=s:"],
                ("sleep", 0.5),
                ["tmux", "send-keys", "-t", "=s:", "Enter"],
            ],
        )
        self.assertEqual(
            fake.kwargs,
            [{"capture_output": True, "text": True, "input": "-x hi"}, TMUX_KW, TMUX_KW],
        )

    def test_text_goes_through_stdin_verbatim(self):
        long_lines = "\n".join(f"line {i:02d} " + "x" * 50 for i in range(28))
        for text in ("ls;", ";", "a\\;", "a;b", long_lines, "one\ntwo;\n"):
            with self.subTest(text=text[:20]):
                fake = Tmux()
                self.send(fake, text)
                self.assertEqual(fake.kwargs[0]["input"], text)
                self.assertNotIn(text, [a for call in fake.calls for a in call])

    def test_a_buffer_per_call(self):
        fake = Tmux()
        first = self.send(fake, "a")
        fake.calls = []
        self.assertNotEqual(self.send(fake, "a"), first)

    def test_failure(self):
        for step, commands in (
            ("load-buffer", ["load-buffer"]),
            ("paste-buffer", ["load-buffer", "paste-buffer", "delete-buffer"]),
        ):
            with self.subTest(step=step):
                fake, sleep = Tmux(fail={step}), unittest.mock.Mock()
                with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
                    tmux_host.send("s", "hi", proc=fake, sleep=sleep)
                self.assertEqual(fake.commands(), commands)
                sleep.assert_not_called()


class PasteTest(Case):
    def test_loads_the_buffer_then_pastes_it_bracketed_deleting_it(self):
        fake = Tmux()
        self.assertIsNone(tmux_host.paste("s", "a;\nb", proc=fake))
        buffer = fake.calls[0][3]
        self.assertRegex(buffer, r"^tui-s-[0-9a-f]{32}$")
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "=s:"],
            ],
        )
        self.assertEqual(
            fake.kwargs, [{"capture_output": True, "text": True, "input": "a;\nb"}, TMUX_KW]
        )

    def test_a_failed_paste_deletes_its_buffer(self):
        for delete in ((0,), (1, "", "no buffer\n"), OSError("gone")):
            with self.subTest(delete=delete):
                fake = Tmux(results={"paste-buffer": (1, "", "boom\n"), "delete-buffer": delete})
                with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
                    tmux_host.paste("s", "hi", proc=fake)
                buffer = fake.calls[0][3]
                self.assertEqual(
                    fake.calls[1:],
                    [
                        ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "=s:"],
                        ["tmux", "delete-buffer", "-b", buffer],
                    ],
                )
        fake = Tmux(fail={"load-buffer"})
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            tmux_host.paste("s", "hi", proc=fake)
        self.assertEqual(fake.commands(), ["load-buffer"])


class KeysTest(Case):
    def test_send_keys_names_the_keys(self):
        fake = Tmux()
        self.assertIsNone(tmux_host.keys("s", "Escape", "C-c", "Enter", proc=fake))
        self.assertEqual(fake.calls, [["tmux", "send-keys", "-t", "=s:", "Escape", "C-c", "Enter"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_a_name_tmux_would_misread_is_refused_before_tmux(self):
        for name in ("-l", "-", "Enter;", ";"):
            with self.subTest(name=name):
                fake = Tmux()
                with self.assertRaisesRegex(types.TuiError, "^tmux would misread"):
                    tmux_host.keys("s", "Escape", name, proc=fake)
                self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            tmux_host.keys("s", "Enter", proc=Tmux(fail={"send-keys"}))


class ReadTest(Case):
    def test_visible_pane_or_last_lines(self):
        for lines, printed, want, extra in (
            (None, "a\n  b\n\n \n", "a\n  b", []),
            (3, "1\n2\n\n3\n4\n\n\n", "\n3\n4", ["-S", "-3"]),
        ):
            with self.subTest(lines=lines):
                fake = Tmux(results={"capture-pane": (0, printed)})
                self.assertEqual(tmux_host.read("s", lines, proc=fake), want)
                self.assertEqual(
                    fake.calls, [["tmux", "capture-pane", "-p", "-J", "-t", "=s:", *extra]]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_lines_must_be_a_positive_int(self):
        fake = Tmux()
        for lines in (0, -1, True, 1.5, "3"):
            with self.subTest(lines=lines), self.assertRaises(types.TuiError):
                tmux_host.read("s", lines, proc=fake)
        self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            tmux_host.read("s", proc=Tmux(fail={"capture-pane"}))


class OptionTest(Case):
    def test_value(self):
        for printed, want in (("0123abcd\n", "0123abcd"), ("a b\n", "a b"), ("\n", "")):
            with self.subTest(printed=printed):
                fake = Tmux(results={"show-options": (0, printed)})
                self.assertEqual(tmux_host.option("s", "@sid", proc=fake), want)
                self.assertEqual(fake.calls, [["tmux", "show-options", "-v", "-t", "=s:", "@sid"]])
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_none_when_unset_or_absent(self):
        for err in ("invalid option: @sid", *ABSENT):
            with self.subTest(err=err):
                fake = Tmux(results={"show-options": (1, "", err + "\n")})
                self.assertIsNone(tmux_host.option("s", "@sid", proc=fake))

    def test_another_failure_raises(self):
        for result in (
            (1, "", FAILED[0] + "\n"),
            (1, "", "boom\n"),
            FileNotFoundError(2, "No such file", "tmux"),
        ):
            with self.subTest(result=result), self.assertRaisesRegex(types.TuiError, "^tmux: "):
                tmux_host.option("s", "@sid", proc=Tmux(results={"show-options": result}))

    def test_only_a_user_option(self):
        for name in ("status", "@", "@a b", "@a;", "", "-g"):
            with self.subTest(name=name):
                fake = Tmux()
                with self.assertRaisesRegex(types.TuiError, "^option "):
                    tmux_host.option("s", name, proc=fake)
                self.assertEqual(fake.calls, [])


class PaneIdTest(Case):
    def test_its_pane(self):
        fake = Tmux(pane="%5")
        self.assertEqual(tmux_host.pane("s", proc=fake), "%5")
        self.assertEqual(fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", "#{pane_id}"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_none_when_absent(self):
        # tmux (3.7) exits 0 printing nothing for a missing session
        for result in (*((1, "", err + "\n") for err in ABSENT), (0, "\n"), (0, "")):
            with self.subTest(result=result):
                self.assertIsNone(
                    tmux_host.pane("s", proc=Tmux(results={"display-message": result}))
                )

    def test_a_failure_or_junk_raises(self):
        for result, error in (
            ((1, "", "boom\n"), "^tmux: boom$"),
            ((1, "", FAILED[0] + "\n"), "^tmux: "),
            ((0, "%5;\n"), "^bad tmux pane '%5;'$"),
            ((0, "5\n"), "^bad tmux pane '5'$"),
            ((0, "%5\n%6\n"), "^bad tmux pane"),
        ):
            with self.subTest(result=result), self.assertRaisesRegex(types.TuiError, error):
                tmux_host.pane("s", proc=Tmux(results={"display-message": result}))


class PaneTargetTest(Case):
    """status, kill, paste, keys, option and read take a pane id (%N) as their target."""

    def calls(self, target, fake):
        return [
            lambda: tmux_host.status(target, proc=fake),
            lambda: tmux_host.kill(target, proc=fake),
            lambda: tmux_host.paste(target, "hi", proc=fake),
            lambda: tmux_host.keys(target, "Enter", proc=fake),
            lambda: tmux_host.option(target, "@sid", proc=fake),
            lambda: tmux_host.read(target, proc=fake),
        ]

    def test_a_pane_id_is_the_target_as_is(self):
        fake = Tmux(pane="0  ")
        for call in self.calls("%12", fake):
            call()
        buffer = fake.calls[2][3]
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "display-message", "-p", "-t", "%12", FORMAT],
                ["tmux", "kill-session", "-t", "%12"],
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "%12"],
                ["tmux", "send-keys", "-t", "%12", "Enter"],
                ["tmux", "show-options", "-v", "-t", "%12", "@sid"],
                ["tmux", "capture-pane", "-p", "-J", "-t", "%12"],
            ],
        )

    def test_a_gone_pane_is_absent(self):
        gone = (1, "", "can't find pane: %12\n")
        self.assertIsNone(tmux_host.status("%12", proc=Tmux(results={"display-message": gone})))
        self.assertIsNone(
            tmux_host.option("%12", "@sid", proc=Tmux(results={"show-options": gone}))
        )
        with self.assertRaisesRegex(types.TuiError, r"^tmux: can't find pane: %12$"):
            tmux_host.kill("%12", proc=Tmux(results={"kill-session": gone}))

    def test_a_bad_target_is_refused_before_tmux(self):
        for target in ("%", "%x", "%1;", "%1 ", "%1:", "%1.0", "=%1", "%-1", "a b"):
            with self.subTest(target=target):
                fake = Tmux()
                for call in self.calls(target, fake):
                    with self.assertRaisesRegex(types.TuiError, "^invalid target "):
                        call()
                self.assertEqual(fake.calls, [])


class StartTest(StartFixture):
    def test_start_runs_wrapper_decorates_dead_pane_without_opening_a_pane(
        self,
    ):
        # in tmux or iTerm2 all the same: start never shows the session
        for env in ({}, INSIDE, ITERM):
            with self.subTest(env=env), environ(**env), which():
                fake = Tmux()
                self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
                self.assertEqual(
                    fake.calls[1],
                    [
                        "tmux",
                        "new-session",
                        "-d",
                        "-s",
                        "s",
                        "-x",
                        "120",
                        "-y",
                        "40",
                        sys.executable,
                        "-I",
                        "-c",
                        tmux_host.EXEC,
                        fake.path,
                        fake.fifo,
                        ";",
                        "set-option",
                        "-p",
                        "-t",
                        "=s:",
                        "remain-on-exit",
                        "on",
                        *chained(decorations("s")),
                    ],
                )
                self.assertNotIn("-c", fake.calls[1][: fake.calls[1].index(sys.executable)])
                self.assertEqual({a for a in fake.calls[1] if a.endswith(";")}, {";"})
                self.assertEqual(fake.kwargs, [TMUX_KW] * len(fake.calls))
                self.sleep.assert_not_called()
                self.assertEqual(self.stderr.getvalue(), "")

    def test_handover_file(self):
        # the env as given but its terminal keys; PWD the cwd, given another (as here) or none
        # (test_events_and_...)
        env = {
            "PATH": self.bin,
            "HOME": "/h",
            "CHILD_SESSION": "1",
            "PWD": "/elsewhere",
            **{k: "x" for k in tmux_host.TERMINAL_KEYS},
        }
        for argv in (("tool", "-x", "a b"), ("tool", "--settings", "s.json")):
            with self.subTest(argv=argv):
                fake = Tmux()
                self.start(fake, argv=argv, env=env)
                self.assertEqual(
                    fake._handover,
                    {
                        "argv": [self.tool, *argv[1:]],
                        "env": {
                            "PATH": self.bin,
                            "HOME": "/h",
                            "CHILD_SESSION": "1",
                            "PWD": self.cwd,
                        },
                        "cwd": self.cwd,
                    },
                )
                self.assertEqual(fake.mode, 0o600)
                self.assertEqual(fake.dir_mode, 0o700)
                self.assertEqual(os.path.dirname(os.path.dirname(fake.path)), self.temp)
                self.assertEqual(os.path.dirname(fake.fifo), os.path.dirname(fake.path))
                self.assertTrue(stat.S_ISFIFO(fake.fifo_mode))
                self.assertEqual(stat.S_IMODE(fake.fifo_mode), 0o600)
                self.assertEqual(os.listdir(self.temp), [])

    def test_terminal_keys(self):
        self.assertEqual(
            set(tmux_host.TERMINAL_KEYS),
            {
                "TMUX",
                "TMUX_PANE",
                "TERM",
                "COLORTERM",
                "TERM_PROGRAM",
                "TERM_PROGRAM_VERSION",
                "TERM_SESSION_ID",
                "ITERM_SESSION_ID",
                "ITERM_PROFILE",
                "LC_TERMINAL",
                "LC_TERMINAL_VERSION",
                "COLUMNS",
                "LINES",
            },
        )

    def test_cwd_never_reaches_tmux(self):
        self.cwd = os.path.join(self.root, "#(x)#{y}")
        os.makedirs(self.cwd)
        fake = Tmux()
        self.start(fake)
        self.assertEqual(fake._handover["cwd"], self.cwd)
        self.assertEqual([a for c in fake.calls for a in c if "#(x)" in a or "#{y}" in a], [])

    def test_launch_rejects_nul_before_tmux(self):
        fake = Tmux()
        with self.assertRaisesRegex(ConfigError, "launch: argv must be strings without NUL"):
            self.start(fake, argv=("tool", "a\0b"))
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_refused_before_tmux(self):
        # the command resolved with the env's PATH (no sh there); an events file that is no regular
        # file
        link = os.path.join(self.root, "link")
        os.symlink(self.tool, link)
        for kw, error in (
            ({"argv": ("sh", "-c", "true")}, "sh"),
            ({"argv": ("no-such-tool",)}, "no-such-tool"),
            ({"argv": ()}, "no command"),
            ({"events": self.cwd}, ""),
            ({"events": link}, ""),
        ):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaisesRegex(types.TuiError, error):
                    self.start(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_unwritable(self):
        fake = Tmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", os.path.join(self.root, "missing")),
            self.assertRaisesRegex(types.TuiError, "^handover: "),
        ):
            self.start(fake)
        real = os.open

        def refuse(path, *args, **kw):
            if str(path).endswith("handover.json"):
                raise PermissionError(13, "Permission denied", path)
            return real(path, *args, **kw)

        with (
            unittest.mock.patch("os.open", side_effect=refuse),
            self.assertRaisesRegex(types.TuiError, "^handover: "),
        ):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(self.temp), [])

    def test_relative_command_made_absolute(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        for argv0, path in (("bin/tool", "/nonexistent"), ("tool", "bin")):
            fake = Tmux()
            self.start(fake, argv=(argv0,), env={"PATH": path})
            self.assertEqual(fake._handover["argv"], [self.tool])

    def test_live_session_is_refused(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env), unittest.mock.patch.dict(os.environ, env):
                fake = Tmux(pane="0  ")
                with self.assertRaisesRegex(
                    types.TuiError,
                    "^" + re.escape(f"session s is running; {prefix}tmux attach -t '=s'") + "$",
                ):
                    self.start(fake)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_dead_session_is_refused_untouched(self):
        for pane in ("1 0 ", "1  9"):
            with self.subTest(pane=pane):
                fake = Tmux(pane=pane)
                with self.assertRaisesRegex(types.TuiError, "^session s exists$"):
                    self.start(fake)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_an_unknown_session_is_never_killed(self):
        # tmux failing otherwise: new-session finds out; another start's session is not ours to kill
        for err in FAILED:
            with self.subTest(err=err):
                fake = Tmux(results={"display-message": (1, "", err)})
                self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
        fake = Tmux(
            results={
                "display-message": (1, "", "boom\n"),
                "new-session": (1, "", "duplicate session: s\n"),
            }
        )
        with self.assertRaisesRegex(types.TuiError, r"^tmux: duplicate session: s$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_tags_are_session_options_set_in_the_new_session_call(self):
        fake = Tmux()
        self.start(fake, tags={"@sid": "0123abcd-x", "@k": "a b#{x}"})
        self.assertEqual(
            fake.calls[1][fake.calls[1].index(fake.fifo) + 1 :],
            [
                ";",
                "set-option",
                "-p",
                "-t",
                "=s:",
                "remain-on-exit",
                "on",
                ";",
                "set-option",
                "-t",
                "=s:",
                "@sid",
                "0123abcd-x",
                ";",
                "set-option",
                "-t",
                "=s:",
                "@k",
                "a b#{x}",
                *chained(decorations("s")),
            ],
        )
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_bad_tags_refused_before_tmux(self):
        for tags in (
            {"sid": "x"},
            {"@": "x"},
            {"@a b": "x"},
            {"@a;": "x"},
            {"@k": "x;"},
            {"@k": 1},
            {"@k": None},
        ):
            with self.subTest(tags=tags):
                fake = Tmux()
                with self.assertRaisesRegex(types.TuiError, "^tag "):
                    self.start(fake, tags=tags)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_a_display_tag_tmux_could_not_list_or_a_nul_is_refused_before_tmux(self):
        for tags, error in (
            ({"@display": "a\tb"}, "^display "),
            ({"@display": "x;"}, "^display "),
            ({"@k": "a\0b"}, "^tag "),
        ):
            with self.subTest(tags=tags):
                fake = Tmux()
                with self.assertRaisesRegex(types.TuiError, error):
                    self.start(fake, tags=tags)
                self.assertEqual(fake.calls, [])

    def test_a_failed_tmux_call_kills_the_session_it_may_have_made(self):
        fake = Tmux(
            fail={"new-session"}
        )  # e.g. the chained set-option failed after new-session made the session
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
        self.assertEqual(os.listdir(self.temp), [])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_a_duplicate_session_is_not_killed(self):
        fake = Tmux(
            results={"new-session": (1, "", "duplicate session: s\n")}
        )  # another start took the name
        with self.assertRaisesRegex(types.TuiError, r"^tmux: duplicate session: s$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_an_interrupt_after_new_session_kills_the_session(self):
        def interrupt(*_args, **_kw):
            raise KeyboardInterrupt

        fake = Tmux(hand_over=False)
        self.sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertIs(fake.dir_at_kill, expr2=False)
        self.assertEqual(os.listdir(self.temp), [])

    def test_tmux_missing(self):
        # status: unknown; new-session: the error
        def missing(_argv, **_kw):
            raise FileNotFoundError(2, "No such file or directory", "tmux")

        with self.assertRaisesRegex(types.TuiError, r"^tmux: "):
            self.start(missing)
        self.assertEqual(os.listdir(self.temp), [])

    def test_wrapper_never_reads(self):
        for fail in ((), {"kill-session"}):
            with self.subTest(fail=fail):
                self.sleep.reset_mock()
                fake = Tmux(fail=fail, hand_over=False)
                with self.assertRaisesRegex(types.TuiError, r"^the session did not start$"):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
                self.assertIs(fake.dir_at_kill, expr2=False)
                self.assertAlmostEqual(
                    sum(c.args[0] for c in self.sleep.call_args_list), tmux_host.HANDOVER_TIMEOUT
                )
                self.assertEqual(os.listdir(self.temp), [])
                self.assertEqual(self.stderr.getvalue(), "")

    def test_waits_for_a_late_wrapper(self):
        fake = Tmux(hand_over=False)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                os.unlink(fake.path)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_waits_for_a_late_exec(self):
        fake, slept = Tmux(wrapper="hang"), []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                os.close(fake.writer)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_an_exec_failure_raises_its_error_and_kills_the_session(self):
        fake = Tmux(wrapper=errno.ENOEXEC)
        with self.assertRaisesRegex(
            types.TuiError,
            "^" + re.escape(f"cannot run {self.tool}: {os.strerror(errno.ENOEXEC)}") + "$",
        ):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_a_handover_at_its_last_poll_still_gets_the_whole_exec_wait(self):
        fake, slept = Tmux(hand_over=False), []
        last = round(tmux_host.HANDOVER_TIMEOUT / tmux_host.POLL)

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == last:
                fake.writer = os.open(fake.fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.unlink(fake.path)
            elif len(slept) == last + 3:
                os.close(fake.writer)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), last + 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_bad_errpipe_data_is_an_error_and_kills_the_session(self):
        for data in (b"x", b"9" * 20):
            with self.subTest(data=data):
                fake = Tmux(wrapper=data)
                with self.assertRaisesRegex(
                    types.TuiError,
                    "^" + re.escape(f"cannot run {self.tool}: bad errpipe data") + "$",
                ):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_never_execd_is_not_started(self):
        fake = Tmux(wrapper="hang")
        with self.assertRaisesRegex(types.TuiError, r"^the session did not start$"):
            self.start(fake)
        os.close(fake.writer)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertAlmostEqual(
            sum(c.args[0] for c in self.sleep.call_args_list), tmux_host.HANDOVER_TIMEOUT
        )
        self.assertEqual(os.listdir(self.temp), [])

    def test_unsafe_tmux_argument(self):
        for exe in ("/opt/py#3/bin/python3", "/opt/bin/python3;"):
            with self.subTest(exe=exe), unittest.mock.patch.object(sys, "executable", exe):
                fake = Tmux()
                with self.assertRaises(types.TuiError):
                    self.start(fake)
                self.assertNotIn("new-session", fake.commands())
        hashed = os.path.join(self.root, "t#mp")
        os.makedirs(hashed)
        fake = Tmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", hashed),
            self.assertRaises(types.TuiError),
        ):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(hashed), [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_invalid_session_name(self):
        for name in ("", "a b", "a.b", "a:b", "=a", "#{x}", "a;", "a\n", "é"):
            with self.subTest(name=name):
                fake = Tmux()
                for call in (
                    lambda *, fake=fake, name=name: self.start(fake, session=name),
                    lambda *, fake=fake, name=name: tmux_host.status(name, proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.detach(
                        name,
                        Launch(("tool",), {"PATH": self.bin}, cwd=self.cwd),
                        proc=fake,
                        sleep=self.sleep,
                    ),
                    lambda *, fake=fake, name=name: tmux_host.kill(name, proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.send(
                        name, "x", proc=fake, sleep=self.sleep
                    ),
                    lambda *, fake=fake, name=name: tmux_host.paste(name, "x", proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.keys(name, "Enter", proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.read(name, proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.option(name, "@sid", proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.pane(name, proc=fake),
                ):
                    with self.assertRaises(types.TuiError):
                        call()
                self.assertEqual(fake.calls, [])

    def test_events_and_decorations(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        events = os.path.join(self.root, "ev")
        # the status line is the caller's choice
        for given, path, status_line, status in (
            (None, None, False, "off"),
            ("ev", events, False, "off"),
            (None, None, True, "on"),
        ):
            with self.subTest(events=given, status_line=status_line):
                fake = Tmux()
                self.start(fake, env={"PATH": self.bin}, events=given, status_line=status_line)
                self.assertEqual(fake._handover["argv"], [self.tool, "-x", "a b"])
                self.assertEqual(fake._handover["env"], {"PATH": self.bin, "PWD": self.cwd})
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
                self.assertEqual(
                    fake.calls[1][fake.calls[1].index(fake.fifo) + 8 :],
                    chained(decorations("s", path, status)),
                )
        self.assertEqual(stat.S_IMODE(os.stat(events).st_mode), 0o600)
