"""Monitor behavior."""

import contextlib
import errno
import functools
import io
import os
import time
from unittest import mock

from agent_pm.manager.engine import crew, roster

from manager.tests.unit.crew.case import CrewCase
from manager.tests.unit.crew.data import SID, SessionView, nl, row
from manager.tests.unit.crew.runtime import Api


class NextEventTest(CrewCase):
    A, B, C = "10:00:01 w1 done", "10:00:02 w2 blocked", "10:00:03 w1 outcome needs_input"

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, "events")

    def append(self, data, path=None):
        with open(path or self.path, "ab") as f:
            f.write(data if isinstance(data, bytes) else data.encode())

    def sleeping(self, *steps):
        calls = iter(steps)

        def sleep(_seconds):
            step = next(calls, None)
            if step is None:
                self.fail("next_event kept waiting")
            step()

        return mock.patch.object(time, "sleep", side_effect=sleep)

    def error(self, reason, after=0, path=None):
        path = path or self.path
        with self.assertRaises(crew.WorkersError) as cm:
            crew.next_event(path, after)
        self.assertEqual(str(cm.exception), f"events file {path}: {reason}")

    def test_every_kind_is_an_event(self):
        kinds = [
            f"10:00:0{i} w1 {k}"
            for i, k in enumerate(("done", "blocked", "dead", "outcome failed", "new x y"))
        ]
        self.append(nl(*kinds))
        with self.sleeping():
            for n, event in enumerate(kinds):
                self.assertEqual(crew.next_event(self.path, n), (n + 1, event))

    def test_the_first_event_after_a_line_waited_for(self):
        A, B, C = self.A, self.B, self.C
        junk = [
            "",
            "garbage",
            "1:00:01 w1 done",
            "10:00:01 w1",
            "10:00:01 w1 ",
            "10:00:01  w1 done",
            " 10:00:01 w1 done",
        ]
        consumed = "0123456789abcdef\n"

        def rewrite():
            with open(self.path, "r+b") as f:
                f.write(b"\n" * len(consumed))

        cases = {
            "end skips what is there": (nl(A, B), [nl(C)], None, (3, C)),
            "end counts complete lines only": (
                nl(A) + "10:00:02 w1 do",
                ["ne\n"],
                None,
                (2, "10:00:02 w1 done"),
            ),
            "end of a missing file is zero": (None, [nl(A)], None, (1, A)),
            "appended while waiting": (nl(A), [nl(B)], 1, (2, B)),
            "fewer lines than after": (nl(A), [nl(B), nl(C)], 2, (3, C)),
            "a partial last line waits for its newline": (
                nl(A) + "10:00:02 w1 caf",
                [b"\xc3", b"\xa9\n"],
                1,
                (2, "10:00:02 w1 café"),
            ),
            "a missing file waited for": (None, [lambda: None, nl(A)], 0, (1, A)),
            "consumed bytes not read again": (consumed, [rewrite, nl(B)], 0, (2, B)),
            "malformed lines skipped but counted": (nl(A, *junk, B), [], 1, (len(junk) + 2, B)),
            "decoded as UTF-8 with replacement": (
                b"10:00:01 w1 caf\xc3\xa9 \xff\n",
                [],
                0,
                (1, "10:00:01 w1 café �"),
            ),
        }
        for why, (data, steps, after, want) in cases.items():
            with self.subTest(why):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
                if data is not None:
                    self.append(data)
                steps = [s if callable(s) else functools.partial(self.append, s) for s in steps]
                with self.sleeping(*steps) as sleep:
                    self.assertEqual(crew.next_event(self.path, after), want)
                self.assertEqual(sleep.call_args_list, [mock.call(0.5)] * len(steps))

    def test_a_shrunk_replaced_or_unreadable_file_stops_it(self):
        def shrink():
            with open(self.path, "wb") as f:
                f.write(nl(self.A).encode())

        def replace():
            with open(self.path + ".new", "wb") as f:
                f.write(nl(self.B, self.A).encode())
            os.replace(self.path + ".new", self.path)

        for reason, steps, after, path in (
            ("shrank", [shrink], 2, None),
            ("replaced", [replace], 2, None),
            (os.strerror(errno.EISDIR), [], 0, self.dir),
        ):
            with self.subTest(reason):
                with open(self.path, "wb") as f:
                    f.write(nl(self.A, self.B).encode())
                with self.sleeping(*steps):
                    self.error(reason, after, path)

    def test_main_prints_line_and_event(self):
        self.attached("--manager", "m1")
        self.api.rows.update(
            w1=row("w1", view=SessionView(state="dead")),
            w2=row("w2", view=SessionView(state="dead")),
        )
        path = roster.events(roster.directory("m1", driver=self.api), create=False)
        self.append(nl(self.A, self.B), path)
        with self.sleeping():
            self.assertEqual(
                self.run_main("next-event", "--manager", "m1", "--after", "1", "--gen", "0"),
                (0, f"2 {self.B}\n", ""),
            )
        with self.sleeping(lambda: self.append(nl(self.C), path)):
            self.assertEqual(
                self.run_main("next-event", "--manager", "m1", "--after", "end", "--gen", "0"),
                (0, f"3 {self.C}\n", ""),
            )

    def test_main_with_a_bad_config(self):
        self.attached("--manager", "m1")
        self.api.rows.update(
            w1=row("w1", view=SessionView(state="dead")),
            w2=row("w2", view=SessionView(state="dead")),
        )
        path = roster.events(roster.directory("m1", driver=self.api), create=False)
        self.append(nl(self.A, self.B), path)
        for text in ("x = \n", "bogus = 1\n"):
            self.local(text)
            with self.subTest(text), self.sleeping():
                self.assertEqual(
                    self.run_main("next-event", "--manager", "m1", "--after", "1", "--gen", "0"),
                    (0, f"2 {self.B}\n", ""),
                )

    def test_main_error_exit(self):
        self.attached("--manager", "m1")
        err = crew.WorkersError(f"events file {self.path}: {os.strerror(errno.EISDIR)}")
        with mock.patch.object(self.event_source, "next", side_effect=err):
            self.assertEqual(
                self.run_main("next-event", "--manager", "m1", "--after", "0", "--gen", "0"),
                (1, "", f"workers: {err}\n"),
            )

    def test_main_usage_error(self):
        for argv in (
            *(["--after", bad, "--gen", "0"] for bad in ("-1", "x", "1.5", "")),
            *(["--after", "1", "--gen", bad] for bad in ("-1", "x", "end", "")),
            ["--after", "1"],
            ["--gen", "0"],
            [],
        ):
            with (
                self.sleeping(),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as cm,
            ):
                crew.main(["next-event", "--manager", "m1", *argv], driver=self.api)
            self.assertEqual(cm.exception.code, 2, argv)


class NextEventContentTest(CrewCase):
    PANE = [f"pane {i}" for i in range(40)]
    DEAD = [f"dead {i}" for i in range(45)]

    def setUp(self):
        super().setUp()
        self.attached("--manager", "m1")
        self.path = roster.events(roster.directory("m1", driver=self.api), create=False)

    def run_event(self, event, *rows):
        self.api.rows.update((r.name, r) for r in rows)
        with open(self.path, "w") as f:
            f.write(event + "\n")
        return self.run_main("next-event", "--manager", "m1", "--after", "0", "--gen", "0")

    def live(self, reply=None):
        self.api.tuis["w1"] = (reply, "\n".join(self.PANE))
        return row("w1", SID)

    def dead(self, name="w1"):
        return row(name, SID, view=SessionView(state="dead", last_lines="\n".join(self.DEAD)))

    def test_worker_done_prints_the_first_80_lines_of_its_reply(self):
        reply = [f"r{i}" for i in range(100)]
        got = self.run_event("10:00:01 w1 done", self.live("\n".join(reply)))
        self.assertEqual(got, (0, nl("1 10:00:01 w1 done", *reply[:80]), ""))
        self.assertEqual(self.api.ran("screen"), [])

    def test_done_without_a_reply_prints_the_pane(self):
        for why, made, lines in (
            ("no turn yet", lambda: self.live(None), self.PANE),
            ("not a tui session, dead", self.dead, self.DEAD[-40:]),
        ):
            with self.subTest(why):
                self.api.tuis.clear()
                got = self.run_event("10:00:01 w1 done", made())
                self.assertEqual(got, (0, nl("1 10:00:01 w1 done", *lines), ""))

    def test_blocked_and_dead_print_the_pane(self):
        for kind in ("blocked", "dead"):
            for state, made, lines in (
                ("live", self.live, self.PANE),
                ("dead", self.dead, self.DEAD[-40:]),
            ):
                with self.subTest(kind=kind, state=state):
                    self.api = Api(None)
                    got = self.run_event(f"10:00:01 w1 {kind}", made())
                    self.assertEqual(got, (0, nl(f"1 10:00:01 w1 {kind}", *lines), ""))
                    self.assertEqual(
                        self.api.ran("screen"), [("w1", 40)] if state == "live" else []
                    )
                    self.assertEqual(self.api.ran("attach"), ["w1"] if state == "live" else [])

    def test_a_dead_pane_prints_only_its_last_40_lines_blank_ones_kept(self):
        lines = ["error", *[""] * 40, "Pane is dead (status 2)"]
        got = self.run_event(
            "10:00:01 w1 dead",
            row("w1", SID, view=SessionView(state="dead", last_lines="\n".join(lines))),
        )
        self.assertEqual(got, (0, nl("1 10:00:01 w1 dead", *lines[-40:]), ""))

    def test_other_events_print_nothing_more(self):
        for event in ("10:00:01 w1-drive outcome done", "10:00:01 w1 new kind"):
            with self.subTest(event=event):
                self.api = Api(None)
                self.assertEqual(self.run_event(event, self.live("r")), (0, nl(f"1 {event}"), ""))
                self.assertEqual(self.api.ran("sessions"), [None])
                self.assertEqual(self.api.ran("attach"), [])

    def test_empty_reply_prints_nothing_more(self):
        got = self.run_event("10:00:01 w1 done", self.live(""))
        self.assertEqual(got, (0, nl("1 10:00:01 w1 done"), ""))
        self.assertEqual(self.api.ran("screen"), [])

    def test_a_done_events_reply_is_read_only_after_configure(self):
        seen, attach = [], self.api.attach
        self.api.attach = lambda name: seen.append(self.api) or attach(name)
        got = self.run_event("10:00:01 w1 done", self.live("r"))
        self.assertEqual(got, (0, nl("1 10:00:01 w1 done", "r"), ""))
        self.assertEqual(seen, [self.api])

    def test_content_uses_the_supplied_driver_despite_an_unrelated_config(self):
        self.local("bogus = 1\n")
        rc, out, err = self.run_event("10:00:01 w1 done", self.live("r"))
        self.assertEqual((rc, out, err), (0, "1 10:00:01 w1 done\nr\n", ""))
        self.assertEqual(self.api.ran("attach"), ["w1"])

    def test_a_content_failure_is_printed_on_stdout_exit_0(self):
        cases = (
            ("10:00:01 w1 done", (), "workers: w1: no tmux session"),
            ("10:00:01 w1 blocked", (row("w1", SID),), "workers: no tui session w1"),
            ("10:00:01 a.b dead", (), "workers: a.b: no tmux session"),
        )
        for event, rows, msg in cases:
            with self.subTest(event=event, msg=msg):
                self.api = Api(None)
                self.assertEqual(self.run_event(event, *rows), (0, nl(f"1 {event}", msg), ""))
