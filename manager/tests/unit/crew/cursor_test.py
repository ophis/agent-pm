"""Cursor behavior."""

import contextlib
import errno
import io
import json
import os
import shlex
from unittest import mock

from agent_pm.manager.engine import crew, roster
from agent_pm.session import (
    DriverError,
)

from manager.tests.case import PYTHONPATH
from manager.tests.unit.crew.case import RosterCase
from manager.tests.unit.crew.data import HEADER, MGR, MODULE, SPAN, nl, numbered, tail, within
from manager.tests.unit.crew.runtime import Api


class CursorTest(RosterCase):
    BIG = SPAN * 11 // 10
    ROTATE = "workers: rotate: run crew.py attach, then arm with its N and GEN\n"

    def setUp(self):
        super().setUp()
        self.mevents = os.path.join(self.mdir, "events")
        self.old = self.mevents + ".1"
        self.waits = []
        self.enterContext(mock.patch.object(self.event_source, "next", side_effect=self.wait))

    def wait(self, events, after):
        self.waits.append((events, after, json.loads(self.bytes())["cursor"]))
        return after + 1, "12:00:00 w1 outcome done"

    def fill(self, data, cursor=0, gen=0):
        before = self.seed(MGR, cursor=cursor, gen=gen)
        with open(self.mevents, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        return before

    def read(self, path=None):
        with open(path or self.mevents, "rb") as f:
            return f.read()

    def cursor(self):
        r = json.loads(self.bytes())
        return r["cursor"], r["gen"]

    def attach(self, *argv, own="mgr"):
        return self.main_with(Api(own), "attach", "--manager", "mgr", *argv)

    def next_event(self, after, gen=0):
        return self.main_with(Api(), "next-event", "--after", str(after), "--gen", str(gen))

    def test_attach_prints_the_backlog_after_the_cursor_then_n_gen_and_the_arm_commands(self):
        events = [f"12:00:0{i} w1 done" for i in range(1, 6)]
        self.fill(nl(*events), cursor=3)
        me = f"env PYTHONPATH={shlex.quote(PYTHONPATH)} python3 -P -m agent_pm.manager.engine.crew"
        self.assertEqual(
            self.attach(),
            (
                0,
                HEADER
                + nl(
                    f"4 {events[3]}",
                    f"5 {events[4]}",
                    "N=3 GEN=0",
                    f"monitor: tail -n +4 -F {self.mevents}",
                    f"monitor expired: {me} attach --manager mgr --after LINE --gen 0 --resume",
                    f"next-event: {me} next-event --manager mgr --after 3 --gen 0",
                ),
                "",
            ),
        )
        self.assertEqual(self.cursor(), (3, 0))

    def test_the_backlog_skips_a_non_event_line_but_counts_it(self):
        self.fill(
            b"12:00:01 w1 done\ngarbage\n\n12:00:04 w1 caf\xc3\xa9 \xff\n"
            b"12:00:05 w2 blocked\n12:00:06 w1 do",
            cursor=1,
        )
        self.assertEqual(
            self.attach(),
            (
                0,
                HEADER + tail(self.mdir, 1, 0, "4 12:00:04 w1 café �", "5 12:00:05 w2 blocked"),
                "",
            ),
        )

    def test_attach_names_the_crew_module_on_its_import_root_then_the_callers_pythonpath(self):
        for given, want in ((PYTHONPATH, PYTHONPATH), (f"{PYTHONPATH}:/u 1", f"{PYTHONPATH}:/u 1")):
            with self.subTest(given=given), mock.patch.dict(os.environ, {"PYTHONPATH": given}):
                rc, out, err = self.attach()
                self.assertEqual((rc, err), (0, ""))
                lines = out.splitlines()[-2:]
                self.assertEqual(
                    [line.split(": ", 1)[0] for line in lines], ["monitor expired", "next-event"]
                )
                self.assertEqual(
                    [shlex.split(line.split(": ", 1)[1])[:6] for line in lines],
                    [["env", f"PYTHONPATH={want}", "python3", *MODULE]] * 2,
                )

    def test_attach_quotes_the_paths_it_prints(self):
        home = os.path.join(self.dir, "it's a home")
        os.mkdir(home)
        root = os.path.join(self.dir, "my src")
        with (
            mock.patch.dict(os.environ, {"HOME": home, "PYTHONPATH": root}),
        ):
            rc, out, err = self.attach()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            [shlex.split(line.split(": ", 1)[1]) for line in out.splitlines()[-3:]],
            [
                [
                    "tail",
                    "-n",
                    "+1",
                    "-F",
                    os.path.join(home, ".agent-pm", "managers", "mgr", "events"),
                ],
                [
                    "env",
                    f"PYTHONPATH={PYTHONPATH}:{root}",
                    "python3",
                    *MODULE,
                    "attach",
                    "--manager",
                    "mgr",
                    "--after",
                    "LINE",
                    "--gen",
                    "0",
                    "--resume",
                ],
                [
                    "env",
                    f"PYTHONPATH={PYTHONPATH}:{root}",
                    "python3",
                    *MODULE,
                    "next-event",
                    "--manager",
                    "mgr",
                    "--after",
                    "0",
                    "--gen",
                    "0",
                ],
            ],
        )

    def test_attach_after_records_the_cursor_which_only_moves_forward(self):
        lines = numbered(9).splitlines()
        self.fill(numbered(9))
        self.assertEqual(
            self.attach("--after", "7", "--gen", "0"),
            (0, HEADER + tail(self.mdir, 7, 0, f"8 {lines[7]}", f"9 {lines[8]}"), ""),
        )
        self.assertEqual(self.next_event(5), (0, "6 12:00:00 w1 outcome done\n", ""))
        self.assertEqual((self.cursor(), self.waits), ((7, 0), [(self.mevents, 5, 7)]))
        self.assertEqual(self.attach()[1].splitlines()[-4], "N=7 GEN=0")
        self.assertEqual(self.cursor(), (7, 0))

    def test_attach_refuses_stale_line_numbers_writing_nothing(self):
        cases = {
            "an old gen": (
                ["--after", "7", "--gen", "0"],
                "stale line numbers (gen 0, now 1): run crew.py attach",
            ),
            "past the end": (
                ["--after", "10", "--gen", "1"],
                f"line 10 is past the end of {self.mevents} (9 lines): run crew.py attach",
            ),
        }
        for why, (argv, msg) in cases.items():
            with self.subTest(why):
                before = self.fill(numbered(9), cursor=2, gen=1)
                self.assertEqual(self.attach(*argv), (1, "", f"workers: {msg}\n"))
                self.assertEqual(self.bytes(), before)

    def test_attach_after_and_gen_go_together_as_numbers(self):
        alone = "crew.py: error: --after and --gen go together\n"
        for argv, want in (
            (["--after", "3"], alone),
            (["--gen", "0"], alone),
            (["--after", "end", "--gen", "0"], None),
            (["--after", "-1", "--gen", "0"], None),
            (["--after", "1", "--gen", "x"], None),
        ):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    crew.main(["attach", "--manager", "mgr", *argv], driver=self.api)
                self.assertEqual(cm.exception.code, 2)
                if want:
                    self.assertTrue(err.getvalue().endswith(want), err.getvalue())
                self.assertEqual(self.made(), [])

    def test_attach_rotates_keeping_the_lines_after_the_cursor(self):
        data = numbered(self.BIG)
        backlog = [f"{i} {line}" for i, line in enumerate(data.splitlines()[-3:], 1)]
        cases = {
            "at the cursor": (self.BIG - 3, []),
            "--after below the cursor": (self.BIG - 3, ["--after", "5", "--gen", "0"]),
            "--after recorded first": (0, ["--after", str(self.BIG - 3), "--gen", "0"]),
        }
        for why, (cursor, argv) in cases.items():
            with self.subTest(why):
                self.sleep.reset_mock()
                self.fill(data, cursor)
                ino = os.lstat(self.mevents).st_ino
                self.assertEqual(
                    self.attach(*argv), (0, HEADER + tail(self.mdir, 0, 1, *backlog), "")
                )
                self.assertEqual(self.cursor(), (0, 1))
                self.assertEqual(
                    (os.lstat(self.old).st_ino, self.read(self.old)), (ino, data.encode())
                )
                self.assertEqual(self.read(), "".join(data.splitlines(keepends=True)[-3:]).encode())
                self.sleep.assert_called_once_with(roster.SETTLE)
        before = self.bytes()
        self.assertEqual(
            self.attach("--after", "2", "--gen", "0"),
            (1, "", "workers: stale line numbers (gen 0, now 1): run crew.py attach\n"),
        )
        self.assertEqual(self.bytes(), before)

    def test_attach_refuses_a_symlinked_events_1_renaming_nothing(self):
        data = numbered(self.BIG).encode()
        before = self.fill(data, self.BIG - 3)
        os.symlink(os.path.join(self.dir, "target"), self.old)
        self.assertEqual(
            self.attach(), (1, "", f"workers: {self.old}: not a regular file owned by you\n")
        )
        self.assertEqual(
            (self.bytes(), self.read(), os.path.islink(self.old)), (before, data, True)
        )

    def test_unknown_sessions_with_a_rotation_due_renames_nothing(self):
        data = numbered(self.BIG).encode()
        before = self.fill(data, self.BIG - 3)
        fake = Api()
        fake.error, fake.error_after = DriverError("tmux sessions unknown: tmux failed"), 1
        self.assertEqual(
            self.main_with(fake, "attach"), (1, "", "workers: tmux sessions unknown: tmux failed\n")
        )
        self.assertEqual(
            (self.bytes(), self.read(), os.path.exists(self.old)), (before, data, False)
        )

    def test_attach_after_a_failed_copy_still_numbers_the_new_events(self):
        data = numbered(self.BIG).encode()
        self.fill(data, self.BIG - 3)
        real = os.open

        def open_(path, flags, *args, **kw):
            if path == self.mevents and flags & os.O_APPEND and flags & os.O_NONBLOCK:
                raise OSError(errno.EIO, "Input/output error")
            return real(path, flags, *args, **kw)

        with mock.patch("os.open", open_):
            rc, out, err = self.attach()
        self.assertEqual((rc, out), (0, HEADER + tail(self.mdir, 0, 1)))
        self.assertEqual(
            err,
            f"manager: rotate: {self.mevents}: Input/output error: lines after {self.BIG - 3} of "
            f"{self.old} not copied\n",
        )
        self.assertEqual((self.cursor(), self.read(self.old), self.read()), ((0, 1), data, b""))

    def test_next_event_records_the_line_before_it_waits(self):
        self.fill(numbered(5) + "12:00:00 w6 do")
        self.assertEqual(self.next_event(3), (0, "4 12:00:00 w1 outcome done\n", ""))
        self.assertEqual(self.next_event("end"), (0, "6 12:00:00 w1 outcome done\n", ""))
        self.assertEqual(self.waits, [(self.mevents, 3, 3), (self.mevents, 5, 5)])

    def test_next_event_refuses_stale_line_numbers_on_stdout_writing_nothing(self):
        cases = {
            "past the end": (
                9,
                2,
                f"line 9 is past the end of {self.mevents} (5 lines): run crew.py attach",
            ),
            "an old gen": (3, 1, "stale line numbers (gen 1, now 2): run crew.py attach"),
        }
        for why, (after, gen, msg) in cases.items():
            with self.subTest(why):
                before = self.fill(numbered(5), cursor=2, gen=2)
                self.assertEqual(self.next_event(after, gen), (1, f"workers: {msg}\n", ""))
                self.assertEqual(self.bytes(), before)
        self.assertEqual(self.waits, [])

    def test_next_event_records_then_refuses_when_a_rotation_is_due(self):
        data = numbered(self.BIG).encode()
        self.fill(data)
        self.assertEqual(self.next_event(self.BIG - 3), (1, self.ROTATE, ""))
        self.assertEqual(
            (self.cursor(), self.waits, self.read(), os.path.exists(self.old)),
            ((self.BIG - 3, 0), [], data, False),
        )

    def test_next_event_waits_on_exactly_events_max_bytes(self):
        self.fill(numbered(SPAN))
        self.assertEqual(self.next_event(SPAN), (0, f"{SPAN + 1} 12:00:00 w1 outcome done\n", ""))
        self.assertEqual(self.waits, [(self.mevents, SPAN, SPAN)])

    def test_unhandled_lines_past_events_max_rotate_only_once_half_is_handled(self):
        data = numbered(SPAN + 1).encode()
        self.fill(data)
        half = SPAN // 2
        for k in (1, 2, 3, half - 1):
            with self.subTest(k=k):
                self.assertEqual(self.next_event(k), (0, f"{k + 1} 12:00:00 w1 outcome done\n", ""))
        rc, out, err = self.attach()
        self.assertEqual((rc, err, out.splitlines()[-4]), (0, "", f"N={half - 1} GEN=0"))
        self.assertEqual((self.read(), os.path.exists(self.old)), (data, False))
        self.assertEqual(self.next_event(half), (1, self.ROTATE, ""))
        self.assertEqual((self.cursor(), self.read()), ((half, 0), data))

    def test_next_event_refuses_a_fifo_events_on_stderr(self):
        before = self.seed(MGR)
        os.mkfifo(self.mevents)
        with within():
            self.assertEqual(
                self.next_event(0),
                (1, "", f"workers: {self.mevents}: not a regular file owned by you\n"),
            )
        self.assertEqual(self.bytes(), before)

    def test_an_interrupted_next_event_leaves_its_line_for_the_next_manager(self):
        data = numbered(15)
        self.fill(data)
        with (
            mock.patch.object(self.event_source, "next", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.next_event(12)
        self.assertEqual(
            self.main_with(Api(), "release"), (0, f"workers: released {self.mdir}\n", "")
        )
        backlog = [f"{i} {line}" for i, line in enumerate(data.splitlines()[12:], 13)]
        self.assertEqual(self.attach(own="m2"), (0, HEADER + tail(self.mdir, 12, 0, *backlog), ""))
        self.assertEqual(json.loads(self.bytes())["holder"]["session"], "m2")
