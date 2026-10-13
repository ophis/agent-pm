"""Roster state, lease, durable files and event cursors without live layer commands."""

import datetime
import fcntl
import io
import json
import os
import stat
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_pm.manager.engine.roster import Roster
from agent_pm.manager.engine.types import Held, RosterError, Spec, Stale, State
from agent_pm.utils import Record, append_line

from manager.tests.case import Case


class RosterTest(Case):
    def setUp(self):
        self.directory = os.path.join(self.agent_pm, "managers", "manager")
        self.roster = Roster(self.directory)
        self.record = Record(
            1,
            "custom",
            "x",
            ("s1", "s2"),
            "sid",
            self.home,
            ("fake-ctl",),
            ("fake-ctl", "resume", "x"),
            "opaque",
            "detail",
        )

    def begin(self, name="x", note=None):
        self.roster.begin(Spec(name, ("fake-ctl", "start", name), note))

    def live(self, name="x", note=None):
        self.begin(name, note)
        self.roster.commit(name, self.record)

    def content(self):
        return json.loads(Path(self.roster.path).read_text())

    def test_begin_commit_preserves_record_and_note(self):
        self.begin(note="keep")
        entry = self.roster.entries()["x"]
        self.assertIs(entry.state, State.STARTING)
        self.assertEqual(entry.argv, ("fake-ctl", "start", "x"))
        self.assertIsNone(entry.record)
        self.roster.commit("x", self.record)
        entry = Roster(self.directory).entries()["x"]
        self.assertEqual(
            (entry.record, entry.state, entry.note, entry.argv),
            (self.record, State.LIVE, "keep", ()),
        )
        self.assertEqual(entry.started, self.content()["entries"]["x"]["started"])

    def test_invalid_intents_write_nothing(self):
        for intent in (
            Spec("bad\nname", ("fake",)),
            Spec("x", ()),
            Spec("x", ("fake",), "bad\n"),
            Spec("x", ("fake",), "a" * 501),
        ):
            with self.subTest(intent=intent), self.assertRaises(RosterError):
                self.roster.begin(intent)
        self.assertFalse(os.path.exists(self.directory))

    def test_duplicate_name_and_missing_entry_fail(self):
        self.begin()
        with self.assertRaisesRegex(RosterError, "already"):
            self.begin()
        for operation in (
            lambda: self.roster.commit("missing", self.record),
            lambda: self.roster.remove("missing"),
            lambda: self.roster.note("missing", "note"),
            lambda: self.roster.mark("missing", State.GONE),
        ):
            with self.assertRaisesRegex(RosterError, "not in roster"):
                operation()

    def test_complete_state_machine(self):
        for target in (State.DONE, State.WAITING, State.GONE):
            with self.subTest(target=target):
                self.live()
                self.roster.mark("x", target)
                self.assertIs(self.roster.entries()["x"].state, target)
                self.roster.remove("x")
        self.live()
        self.roster.mark("x", State.GONE)
        self.roster.mark("x", State.LIVE)
        self.roster.mark("x", State.GONE)
        self.roster.mark("x", State.RESUMING)
        self.roster.mark("x", State.GONE)
        self.roster.mark("x", State.RESUMING)
        self.roster.commit("x", self.record)
        self.assertIs(self.roster.entries()["x"].state, State.LIVE)

    def test_invalid_transitions_are_atomic(self):
        self.live()
        self.roster.mark("x", State.DONE)
        before = Path(self.roster.path).read_bytes()
        for state in State:
            if state == State.DONE:
                continue
            with self.subTest(state=state), self.assertRaises(RosterError):
                self.roster.mark("x", state)
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        with self.assertRaises(RosterError):
            self.roster.commit("x", self.record)
        with self.assertRaises(RosterError):
            self.roster.mark("x", "unknown")

    def test_lost_adoption_retains_note_and_started(self):
        self.begin(note="keep")
        self.roster.mark("x", State.LOST)
        old = self.roster.entries()["x"]
        self.assertEqual(self.roster.adopt(self.record), "x")
        adopted = self.roster.entries()["x"]
        self.assertEqual(
            (adopted.state, adopted.record, adopted.note, adopted.started),
            (State.LIVE, self.record, old.note, old.started),
        )

    def test_generic_record_names_are_safe_roster_keys(self):
        for name in ("external.run", "external run", "external/run", "../external", "外部"):
            with self.subTest(name=name):
                record = replace(self.record, name=name, sessions=(name,))
                self.assertEqual(self.roster.adopt(record), name)
                self.assertEqual(Roster(self.directory).entries()[name].record, record)
        self.assertFalse((Path(self.directory).parent / "external").exists())

    def test_start_aliases_accept_printable_names_and_reject_controls(self):
        self.begin("alias / with spaces")
        self.assertIn("alias / with spaces", self.roster.entries())
        for name in ("", "bad\tname", "bad\nname"):
            with self.subTest(name=name), self.assertRaises(RosterError):
                self.begin(name)

    def test_adopt_new_orphan_and_existing_alias_are_idempotent(self):
        self.assertEqual(self.roster.adopt(self.record), "x")
        before = Path(self.roster.path).read_bytes()
        self.assertEqual(self.roster.adopt(self.record), "x")
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.roster.remove("x")
        self.live("alias")
        self.assertEqual(self.roster.adopt(self.record), "alias")
        self.assertEqual(list(self.roster.entries()), ["alias"])

    def test_note_can_be_replaced_or_cleared(self):
        self.live(note="first")
        self.roster.note("x", "second")
        self.assertEqual(self.roster.entries()["x"].note, "second")
        self.roster.note("x", None)
        self.assertIsNone(self.roster.entries()["x"].note)

    def test_resume_claim_is_atomic_across_instances(self):
        self.live()
        self.roster.mark("x", State.GONE)
        claims = []
        barrier = threading.Barrier(2)

        def claim():
            other = Roster(self.directory)
            barrier.wait()
            claims.append(other.claim_resume("x"))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sum(entry is not None for entry in claims), 1)
        self.assertIs(self.roster.entries()["x"].state, State.RESUMING)

    def test_hold_rejects_live_holder_then_takes_dead_holder(self):
        alive = {"a"}
        self.roster.hold("a", alive.__contains__)
        other = Roster(self.directory)
        with self.assertRaises(Held):
            other.hold("b", alive.__contains__)
        alive.clear()
        other.hold("b", alive.__contains__)
        self.assertEqual(self.content()["holder"]["session"], "b")
        with self.assertRaises(RosterError):
            self.begin()
        other.begin(Spec("x", ("fake",)))

    def test_every_mutation_checks_current_lease(self):
        self.roster.hold("a", lambda _: False)
        self.live()
        other = Roster(self.directory)
        other.hold("b", lambda _: False)
        for operation in (
            lambda: self.roster.begin(Spec("y", ("fake",))),
            lambda: self.roster.commit("x", self.record),
            lambda: self.roster.mark("x", State.GONE),
            lambda: self.roster.claim_resume("x"),
            lambda: self.roster.remove("x"),
            lambda: self.roster.note("x", "note"),
            lambda: self.roster.adopt(self.record),
            lambda: self.roster.advance(0, 0),
            self.roster.rotate_events,
            self.roster.release,
            self.roster.check_lease,
            lambda: self.roster.events,
        ):
            with self.subTest(operation=operation), self.assertRaises(RosterError):
                operation()

    def test_bound_owner_requires_attach_and_release_clears_lease(self):
        self.roster.bind("a", lambda _: False)
        with self.assertRaisesRegex(RosterError, "not attached"):
            self.begin()
        self.roster.hold("a", lambda _: False)
        self.begin()
        self.roster.release()
        self.assertIsNone(self.content()["holder"])
        with self.assertRaises(RosterError):
            self.roster.check_lease()

    def test_no_owner_retains_unleased_operations(self):
        self.roster.hold(None, lambda _: False)
        self.live()
        self.roster.check_lease()
        self.roster.release()
        self.assertIsNone(self.content()["holder"])

    def test_private_directory_and_regular_files(self):
        self.live()
        events = self.roster.events
        self.assertEqual(stat.S_IMODE(os.stat(self.directory).st_mode), 0o700)
        for path in (self.roster.path, events, os.path.join(self.directory, "roster.lock")):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_roster_symlink_fifo_and_foreign_file_are_refused(self):
        self.live()
        target = Path(self.home) / "outside"
        target.write_text("keep")
        Path(self.roster.path).unlink()
        os.symlink(target, self.roster.path)
        with self.assertRaises(RosterError):
            self.roster.entries()
        self.assertEqual(target.read_text(), "keep")
        Path(self.roster.path).unlink()
        os.mkfifo(self.roster.path)
        with self.assertRaises(RosterError):
            self.roster.entries()
        Path(self.roster.path).unlink()
        self.live()
        with (
            mock.patch.object(os, "getuid", return_value=os.getuid() + 1),
            self.assertRaises(RosterError),
        ):
            self.roster.entries()

    def test_parent_symlink_is_refused_before_creating_child(self):
        outside = Path(self.home) / "outside"
        outside.mkdir()
        Path(self.directory).parent.symlink_to(outside)
        with self.assertRaises(RosterError):
            self.begin()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(Path(self.directory).exists())

    def test_restrictive_umask_preserves_private_usable_new_directories(self):
        previous = os.umask(0o077)
        try:
            self.live()
            self.assertEqual(self.roster.entries()["x"].record, self.record)
            for path in (Path(self.directory).parent, Path(self.directory)):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        finally:
            os.umask(previous)

    def test_lock_symlink_and_fifo_are_refused_without_waiting(self):
        self.live()
        lock = Path(self.directory) / "roster.lock"
        target = Path(self.home) / "outside"
        target.write_text("keep")
        lock.unlink()
        lock.symlink_to(target)
        with self.assertRaises(RosterError):
            self.roster.entries()
        self.assertEqual(target.read_text(), "keep")
        lock.unlink()
        os.mkfifo(lock)
        with self.assertRaises(RosterError):
            self.roster.entries()

    def test_failed_save_leaves_previous_roster_and_no_temp(self):
        self.live()
        before = Path(self.roster.path).read_bytes()
        with (
            mock.patch.object(os, "replace", side_effect=OSError("disk full")),
            self.assertRaises(RosterError),
        ):
            self.roster.note("x", "new")
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(sorted(os.listdir(self.directory)), ["roster.json", "roster.lock"])

    def test_directory_symlink_and_public_directory_are_refused(self):
        target = Path(self.home) / "target"
        target.mkdir()
        Path(self.directory).parent.mkdir()
        os.symlink(target, self.directory)
        with self.assertRaises(RosterError):
            self.begin()
        Path(self.directory).unlink()
        os.mkdir(self.directory, 0o755)
        with self.assertRaises(RosterError):
            self.begin()

    def test_invalid_json_schema_and_size_are_refused(self):
        self.live()
        valid = Path(self.roster.path).read_text()
        for raw in (
            "{",
            "[]",
            "{}",
            valid.replace('"live"', '"impossible"'),
            " " * ((1 << 20) + 1),
        ):
            Path(self.roster.path).write_text(raw)
            with self.subTest(raw=raw[:20]), self.assertRaises(RosterError):
                self.roster.entries()

    def test_lock_waits_for_other_writer(self):
        self.begin()
        fd = os.open(os.path.join(self.directory, "roster.lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        thread = threading.Thread(target=lambda: self.roster.note("x", "after"))
        thread.start()
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        fcntl.flock(fd, fcntl.LOCK_UN)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.roster.entries()["x"].note, "after")

    def test_events_ignore_partial_line_and_advance_is_monotonic(self):
        path = self.roster.events
        append_line(path, "one\ntwo\npartial")
        self.assertEqual(self.roster.backlog(), (0, 0, ["one", "two"]))
        self.assertEqual(self.roster.next_event(0, 0), (1, "one"))
        self.assertEqual(self.roster.next_event(2, 0), (2, None))
        self.assertEqual(self.roster.advance(None, 0), 2)
        self.roster.advance(1, 0)
        self.assertEqual(self.roster.backlog(), (2, 0, []))
        append_line(path, " complete\n")
        self.assertEqual(self.roster.backlog(), (2, 0, ["partial complete"]))

    def test_bad_cursor_and_generation_are_stale(self):
        self.assertTrue(os.path.isfile(self.roster.events))
        for after, gen in ((1, 0), (-1, 0), (0, 1)):
            with self.subTest(after=after, gen=gen), self.assertRaises(Stale):
                self.roster.advance(after, gen)

    def test_rotation_retains_unread_and_rejects_old_generation(self):
        path = self.roster.events
        append_line(path, "h" * (1 << 20) + "\nunread\n")
        self.assertFalse(self.roster.rotate_events())
        self.roster.advance(1, 0)
        self.assertTrue(self.roster.rotate_events())
        self.assertEqual(self.roster.backlog(), (0, 1, ["unread"]))
        self.assertTrue(os.path.exists(path + ".1"))
        with self.assertRaises(Stale):
            self.roster.next_event(1, 0)

    def test_rotation_preserves_bytes_and_completes_partial_tail(self):
        path = self.roster.events
        append_line(path, b"h" * (1 << 20) + b"\ninvalid\xff partial")
        self.roster.advance(1, 0)
        self.assertTrue(self.roster.rotate_events())
        self.assertEqual(Path(path).read_bytes(), b"invalid\xff partial\n")

    def test_failed_rotation_refill_keeps_archive_and_updates_generation(self):
        path = self.roster.events
        data = b"h" * (1 << 20) + b"\nunread\n"
        append_line(path, data)
        self.roster.advance(1, 0)
        warning = io.StringIO()
        with (
            mock.patch.object(os, "write", side_effect=OSError("disk full")),
            mock.patch("sys.stderr", warning),
        ):
            self.assertTrue(self.roster.rotate_events())
        self.assertEqual(Path(path + ".1").read_bytes(), data)
        self.assertEqual(self.roster.backlog(), (0, 1, []))
        self.assertIn(path + ".1", warning.getvalue())
        self.assertIn("after 1", warning.getvalue())

    def test_next_event_requires_attach_when_rotation_due(self):
        path = self.roster.events
        append_line(path, "h" * (1 << 20) + "\nunread\n")
        self.roster.advance(1, 0)
        with self.assertRaisesRegex(Stale, "rotation due"):
            self.roster.next_event(1, 0)

    def test_rotation_copies_append_to_inflight_descriptor_and_new_path(self):
        path = self.roster.events
        append_line(path, "h" * (1 << 20) + "\nold\n")
        self.roster.advance(1, 0)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        self.addCleanup(os.close, fd)
        barrier = threading.Event()

        def append():
            deadline = time.monotonic() + 5
            while not os.path.exists(path + ".1") and time.monotonic() < deadline:
                time.sleep(0.01)
            os.write(fd, b"inflight\n")
            append_line(path, "new\n")
            barrier.set()

        thread = threading.Thread(target=append)
        thread.start()
        self.assertTrue(self.roster.rotate_events())
        thread.join(5)
        self.assertTrue(barrier.is_set())
        self.assertCountEqual(self.roster.backlog()[2], ["old", "inflight", "new"])

    def test_events_symlink_and_fifo_are_refused(self):
        path = self.roster.events
        target = Path(self.home) / "outside"
        target.write_text("keep")
        Path(path).unlink()
        os.symlink(target, path)
        with self.assertRaises(RosterError):
            self.roster.backlog()
        self.assertEqual(target.read_text(), "keep")
        Path(path).unlink()
        os.mkfifo(path)
        with self.assertRaises(RosterError):
            self.roster.backlog()

    def test_mark_resuming_sets_new_start_time(self):
        self.live()
        data = self.content()
        old = "2020-01-01T00:00:00+00:00"
        data["entries"]["x"]["started"] = old
        Path(self.roster.path).write_text(json.dumps(data))
        self.roster.mark("x", State.GONE)
        self.roster.claim_resume("x")
        started = self.roster.entries()["x"].started
        self.assertGreater(
            datetime.datetime.fromisoformat(started), datetime.datetime.fromisoformat(old)
        )


if __name__ == "__main__":
    unittest.main()
