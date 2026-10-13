"""Cursor behavior."""

import contextlib
import errno
import io
import os
import re
import stat
import types
import unittest
import unittest.mock

from agent_pm.manager.engine import roster

from manager.tests.unit.roster.case import Base
from manager.tests.unit.roster.data import NOT_REGULAR, SPAN, WIDTH, WRITER, mode, numbered, within


class CursorTest(Base):
    def setUp(self):
        super().setUp()
        self.events = os.path.join(self.dir, "events")
        self.old = self.events + ".1"
        self.path = os.path.join(self.dir, "roster.json")
        roster.ensure(self.dir)

    def doc(self, cursor=0, gen=0):
        return {"version": 1, "holder": None, "cursor": cursor, "gen": gen, "entries": {}}

    def write(self, data, path=None):
        with open(path or self.events, "wb") as f:
            f.write(data)
        os.chmod(path or self.events, 0o600)
        return data

    def read(self, path=None):
        with open(path or self.events, "rb") as f:
            return f.read()

    def due_file(self, tail=b""):
        self.write(numbered(SPAN + 3) + tail)
        return SPAN

    def rotate(self, r, during=lambda: None):
        slept, err = [], io.StringIO()

        def sleep(seconds):
            slept.append(seconds)
            during()

        with contextlib.redirect_stderr(err):
            result = roster.rotate(r, self.dir, sleep=sleep)
        return result, slept, err.getvalue()

    def lstat_as(self, path, change):
        real = os.lstat

        def lstat(p, *args, **kw):
            st = real(p, *args, **kw)
            return change(p, st) if p == path else st

        return unittest.mock.patch("os.lstat", lstat)

    def after_rename(self, step):
        real = os.replace

        def replace(src, dst, *args, **kw):
            real(src, dst, *args, **kw)
            if dst == self.old:
                step()

        return unittest.mock.patch("os.replace", replace)

    def test_missing_or_empty_has_no_lines(self):
        for label, make in (("missing", lambda: None), ("empty", lambda: self.write(b""))):
            with self.subTest(label):
                make()
                self.assertEqual(roster.lines(self.events), 0)
                self.assertEqual([roster.offset(self.events, n) for n in (0, 3)], [0, 0])
                self.assertEqual([roster.following(self.events, n) for n in (0, 3)], [b"", b""])

    def test_a_trailing_fragment_is_not_a_line_and_follows_as_is(self):
        self.write(b"a\nbb\nccc")
        self.assertEqual(roster.lines(self.events), 2)
        self.assertEqual([roster.offset(self.events, n) for n in range(5)], [0, 2, 5, 8, 8])
        self.assertEqual(
            [roster.following(self.events, n) for n in range(4)],
            [b"a\nbb\nccc", b"bb\nccc", b"ccc", b""],
        )

    def test_lines_offset_and_following_across_blocks(self):
        data = self.write(numbered(3 * SPAN // 2))
        self.assertEqual(roster.lines(self.events), 3 * SPAN // 2)
        self.assertEqual(roster.offset(self.events, 12345), 12345 * WIDTH)
        self.assertEqual(roster.following(self.events, 12345), data[12345 * WIDTH :])
        self.assertEqual(
            roster.following(self.events, 3 * SPAN // 2 - 2), numbered(2, start=3 * SPAN // 2 - 1)
        )

    def test_symlink_and_fifo_refused_without_blocking(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        with open(target, "wb") as f:
            f.write(b"a\nb\n")
        reads = (roster.lines, lambda p: roster.offset(p, 1), lambda p: roster.following(p, 1))
        os.symlink(target, self.events)
        for kind in ("symlink", "fifo"):
            if kind == "fifo":
                os.unlink(self.events)
                os.mkfifo(self.events)
            for read in reads:
                with self.subTest(kind), within(), self.assertRaises(roster.RosterError) as cm:
                    read(self.events)
                self.assertEqual(str(cm.exception), NOT_REGULAR.format(self.events))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"a\nb\n")

    def test_due_boundaries(self):
        half = roster.EVENTS_MAX // 2
        cases = [
            ("exactly EVENTS_MAX, all handled", numbered(SPAN), SPAN, roster.EVENTS_MAX, False),
            (
                "over, handled one byte short",
                numbered(SPAN + 1, first=WIDTH - 1),
                SPAN // 2,
                half - 1,
                False,
            ),
            ("over, handled exactly half", numbered(SPAN + 1), SPAN // 2, half, True),
            ("over, cursor 0", numbered(2 * SPAN), 0, 0, False),
        ]
        for label, data, cursor, handled, want in cases:
            with self.subTest(label):
                self.write(data)
                self.assertEqual(roster.offset(self.events, cursor), handled)
                r = self.doc(cursor)
                self.assertIs(roster.due(r, self.dir), want)
                self.assertEqual(r, self.doc(cursor))
        os.unlink(self.events)
        self.assertIs(roster.due(self.doc(5), self.dir), expr2=False)

    def test_advance_checks_the_gen_first(self):
        self.write(numbered(5))
        r = self.doc(2, 1)
        for after in (3, 9, None):
            with self.subTest(after=after), self.assertRaises(roster.Stale) as cm:
                roster.advance(r, self.dir, after, 0)
            self.assertEqual(
                str(cm.exception), "stale line numbers (gen 0, now 1): run crew.py attach"
            )
        self.assertEqual(r, self.doc(2, 1))

    def test_advance_past_the_end_is_stale(self):
        self.write(numbered(5) + b"line 6")
        r = self.doc(2, 1)
        with self.assertRaises(roster.Stale) as cm:
            roster.advance(r, self.dir, 6, 1)
        self.assertEqual(
            str(cm.exception),
            f"line 6 is past the end of {self.events} (5 lines): run crew.py attach",
        )
        self.assertEqual(r, self.doc(2, 1))
        os.unlink(self.events)
        with self.assertRaisesRegex(roster.Stale, r"^line 1 is past the end of .* \(0 lines\)"):
            roster.advance(r, self.dir, 1, 1)

    def test_advance_moves_the_cursor_forward_only_and_returns_the_line(self):
        self.write(numbered(5) + b"line 6")
        cases = [
            (0, 3, 3, 3),
            (3, 1, 3, 1),
            (3, 4, 4, 4),
            (3, 5, 5, 5),
            (0, None, 5, 5),
            (5, None, 5, 5),
            (0, 0, 0, 0),
        ]
        for cursor, after, moved, resolved in cases:
            with self.subTest(cursor=cursor, after=after):
                r = self.doc(cursor, 2)
                self.assertEqual(roster.advance(r, self.dir, after, 2), resolved)
                self.assertEqual(r, self.doc(moved, 2))

    def test_advance_on_a_missing_events_file(self):
        r = self.doc()
        self.assertEqual([roster.advance(r, self.dir, after, 0) for after in (0, None)], [0, 0])
        self.assertEqual(r, self.doc())

    def test_rotate_not_due_touches_nothing(self):
        for label, data, cursor in (
            ("exactly EVENTS_MAX", numbered(SPAN), SPAN),
            ("cursor 0", numbered(2 * SPAN), 0),
            ("missing", None, 0),
        ):
            with self.subTest(label):
                if data is None:
                    os.unlink(self.events)
                else:
                    self.write(data)
                r = self.doc(cursor, 2)
                self.assertEqual(self.rotate(r), (False, [], ""))
                self.assertEqual(r, self.doc(cursor, 2))
                self.assertEqual(os.listdir(self.dir), [] if data is None else ["events"])
                if data is not None:
                    self.assertEqual(self.read(), data)

    def test_rotate_keeps_the_lines_after_the_cursor_in_a_fresh_0600_events(self):
        self.addCleanup(os.umask, os.umask(0o022))
        cases = [
            ("plain", b"", False, b""),
            ("a trailing fragment gets its newline", b"12:00:01 w9 do", False, b"12:00:01 w9 do\n"),
            ("an old events.1 replaced", b"", True, b""),
        ]
        for label, tail, old, ended in cases:
            with self.subTest(label):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.old)
                cursor = self.due_file(tail)
                if old:
                    self.write(b"old\n", self.old)
                original, ino = self.read(), os.lstat(self.events).st_ino
                r = self.doc(cursor, 2)
                self.assertEqual(self.rotate(r), (True, [roster.SETTLE], ""))
                self.assertEqual(r, self.doc(0, 3))
                self.assertEqual((os.lstat(self.old).st_ino, self.read(self.old)), (ino, original))
                self.assertEqual(self.read(), numbered(3, start=SPAN + 1) + ended)
                self.assertTrue(stat.S_ISREG(os.lstat(self.events).st_mode))
                self.assertEqual(mode(self.events), 0o600)
                self.assertNotEqual(os.lstat(self.events).st_ino, ino)
                self.assertEqual(sorted(os.listdir(self.dir)), ["events", "events.1"])

    def test_rotate_puts_a_line_appended_during_the_wait_first(self):
        cursor = self.due_file()

        def append():
            with open(self.events, "ab") as f:
                f.write(WRITER)

        r = self.doc(cursor)
        self.assertEqual(self.rotate(r, append)[:2], (True, [roster.SETTLE]))
        self.assertEqual(self.read(), WRITER + numbered(3, start=SPAN + 1))

    def test_rotate_makes_a_writers_0644_events_0600(self):
        self.addCleanup(os.umask, os.umask(0o022))
        cursor = self.due_file()
        made = []

        def write():
            with open(self.events, "ab") as f:
                f.write(WRITER)
            made.append(mode(self.events))

        with self.after_rename(write):
            self.assertIs(self.rotate(self.doc(cursor))[0], expr2=True)
        self.assertEqual(made, [0o644])
        self.assertEqual(mode(self.events), 0o600)
        self.assertEqual(self.read(), WRITER + numbered(3, start=SPAN + 1))

    def test_rotate_refills_no_symlink_or_fifo_made_after_the_rename(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        cases = [
            ("a symlink", lambda: os.symlink(target, self.events), NOT_REGULAR.format(self.events)),
            ("a fifo", lambda: os.mkfifo(self.events), None),
        ]
        for label, make, why in cases:
            with self.subTest(label):
                for p in (self.events, self.old):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(p)
                with open(target, "wb"):
                    pass
                cursor = self.due_file()
                r = self.doc(cursor, 2)
                with self.after_rename(make), within():
                    result, slept, err = self.rotate(r)
                self.assertEqual((result, slept, r), (True, [], self.doc(0, 3)))
                tail = f": lines after {cursor} of {self.old} not copied\n"
                if why is None:
                    self.assertRegex(
                        err, f"^manager: rotate: {re.escape(self.events)}: .+{re.escape(tail)}$"
                    )
                else:
                    self.assertEqual(err, f"manager: rotate: {why}{tail}")
                self.assertEqual(os.path.getsize(target), 0)

    def test_rotate_failing_after_the_rename_still_resets_the_cursor_and_bumps_the_gen(self):
        cursor = self.due_file()
        original = self.read()
        with roster.locked(self.dir) as r:
            r.update(cursor=cursor, gen=2)
        real = os.open

        def open_(path, flags, *args, **kw):
            if path == self.events and flags & os.O_APPEND and flags & os.O_NONBLOCK:
                raise OSError(errno.EIO, "Input/output error")
            return real(path, flags, *args, **kw)

        err = io.StringIO()
        with (
            unittest.mock.patch("os.open", open_),
            contextlib.redirect_stderr(err),
            roster.locked(self.dir) as r,
        ):
            self.assertIs(roster.rotate(r, self.dir, sleep=lambda _seconds: None), expr2=True)
        self.assertEqual(
            err.getvalue(),
            f"manager: rotate: {self.events}: Input/output error: lines after {cursor} of "
            f"{self.old} not copied\n",
        )
        with roster.locked(self.dir, write=False) as r:
            self.assertEqual((r["cursor"], r["gen"]), (0, 3))
        self.assertEqual((self.read(self.old), self.read()), (original, b""))
        self.assertEqual(mode(self.events), 0o600)

    def test_rotate_failing_to_rename_changes_nothing(self):
        cursor = self.due_file()
        original = self.read()
        r = self.doc(cursor, 2)
        with (
            unittest.mock.patch("os.replace", side_effect=OSError(errno.EIO, "Input/output error")),
            self.assertRaises(roster.RosterError) as cm,
        ):
            self.rotate(r)
        self.assertEqual(str(cm.exception), f"{self.events}: Input/output error")
        self.assertEqual(r, self.doc(cursor, 2))
        self.assertEqual((os.listdir(self.dir), self.read()), (["events"], original))

    def test_rotate_refuses_a_symlink_fifo_or_foreign_events_or_events_1(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")

        def foreign(_path, st):
            return types.SimpleNamespace(
                st_mode=st.st_mode, st_uid=st.st_uid + 1, st_size=st.st_size
            )

        cases = [
            ("events.1 a symlink", self.old, lambda: os.symlink(target, self.old), None),
            ("events.1 a fifo", self.old, lambda: os.mkfifo(self.old), None),
            ("events.1 foreign", self.old, lambda: self.write(b"old\n", self.old), foreign),
            ("events foreign", self.events, lambda: None, foreign),
            (
                "events a symlink, lstat seeing its target",
                self.events,
                lambda: (os.rename(self.events, target), os.symlink(target, self.events)),
                lambda path, _st: os.stat(path),
            ),
        ]
        for label, path, make, change in cases:
            with self.subTest(label):
                for p in (self.events, self.old, target):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(p)
                cursor = self.due_file()
                make()
                inodes = {
                    name: os.lstat(os.path.join(self.dir, name)).st_ino
                    for name in os.listdir(self.dir)
                }
                r = self.doc(cursor, 2)
                with (
                    self.lstat_as(path, change) if change else contextlib.nullcontext(),
                    within(),
                    self.assertRaises(roster.RosterError) as cm,
                ):
                    self.rotate(r)
                self.assertEqual(str(cm.exception), NOT_REGULAR.format(path))
                self.assertEqual(r, self.doc(cursor, 2))
                self.assertEqual(
                    {
                        name: os.lstat(os.path.join(self.dir, name)).st_ino
                        for name in os.listdir(self.dir)
                    },
                    inodes,
                )

    def test_a_refusal_inside_roster_leaves_roster_json_unchanged(self):
        cursor = self.due_file()
        with roster.locked(self.dir) as r:
            r.update(cursor=cursor, gen=2)
        with open(self.path, "rb") as f:
            raw = f.read()
        os.symlink(os.path.join(os.path.dirname(self.agent_pm), "target"), self.old)
        with self.assertRaises(roster.RosterError), roster.locked(self.dir) as r:
            roster.rotate(r, self.dir, sleep=lambda _seconds: None)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), raw)
        self.assertTrue(os.path.islink(self.old))
