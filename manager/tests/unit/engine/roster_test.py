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
from uuid import uuid4

from agent_pm.manager.engine.roster import Roster
from agent_pm.manager.engine.types import Held, Lease, RosterError, Spec, Stale, State
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
            str(uuid4()),
            self.home,
            ("fake-ctl",),
            ("fake-ctl", "resume", "x"),
            "opaque",
            "detail",
        )

    def begin(self, name="x", note=None):
        self.roster.begin(Spec(name, ("fake-ctl", "start", name), note), sid=self.record.sid)

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
                self.roster.begin(intent, sid=self.record.sid)
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

    def test_adopt_rejects_noncanonical_sid_before_creating_files(self):
        for sid in (
            None,
            "not-a-uuid",
            "12345678-1234-1234-8234-123456789abc",
            "12345678-1234-4234-8234-123456789ABC",
            "{12345678-1234-4234-8234-123456789abc}",
            "12345678123442348234123456789abc",
        ):
            with self.subTest(sid=sid), self.assertRaisesRegex(RosterError, "canonical uuid4"):
                self.roster.adopt(replace(self.record, sid=sid))
            self.assertFalse(Path(self.directory).exists())

    def test_persisted_invalid_sid_is_refused_for_every_state(self):
        self.roster.adopt(self.record)
        valid = self.content()
        for state in State:
            for sid in (None, "not-a-uuid"):
                with self.subTest(state=state, sid=sid):
                    data = json.loads(json.dumps(valid))
                    entry = data["entries"]["x"]
                    entry["state"], entry["sid"] = state, sid
                    if state in {State.STARTING, State.LOST}:
                        entry["record"], entry["argv"] = None, ["fake-ctl", "start", "x"]
                    else:
                        entry["record"]["sid"] = sid
                    Path(self.roster.path).write_text(json.dumps(data))
                    before = Path(self.roster.path).read_bytes()
                    with self.assertRaisesRegex(RosterError, "canonical uuid4"):
                        self.roster.entries()
                    self.assertEqual(Path(self.roster.path).read_bytes(), before)
                    self.assertEqual(list(Path(self.directory).glob("receipt-*.stdout")), [])

    def test_resume_preflight_rejects_invalid_sid_without_receipt_or_write(self):
        self.roster.adopt(self.record)
        self.roster.mark("x", State.GONE)
        data = self.content()
        data["entries"]["x"]["sid"] = None
        data["entries"]["x"]["record"]["sid"] = None
        Path(self.roster.path).write_text(json.dumps(data))
        before = Path(self.roster.path).read_bytes()
        for operation in (
            lambda: self.roster.claim_resume("x"),
            lambda: self.roster.mark("x", State.RESUMING),
        ):
            with self.assertRaisesRegex(RosterError, "canonical uuid4"):
                operation()
            self.assertEqual(Path(self.roster.path).read_bytes(), before)
            self.assertEqual(list(Path(self.directory).glob("receipt-*.stdout")), [])

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
                record = replace(self.record, name=name, sessions=(name,), sid=str(uuid4()))
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
        other = Roster(self.directory)
        with self.roster.hold("a") as first:
            with self.assertRaises(Held), self.roster.hold("nested"):
                self.fail("live holder replaced")
            self.roster.check_lease()
            with self.assertRaises(Held), other.hold("b"):
                self.fail("live holder replaced")
            with self.assertRaises(Held):
                other.begin(Spec("x", ("fake",)), sid=self.record.sid)
        with other.hold("b") as second:
            self.assertGreater(second.generation, first.generation)
            self.assertNotEqual(second.id, first.id)
            self.assertEqual(self.content()["holder"]["session"], "b")
            with self.assertRaises(Held):
                self.begin()
            other.begin(Spec("x", ("fake",)), sid=self.record.sid)

    def test_every_mutation_checks_current_lease(self):
        other = Roster(self.directory)
        with self.roster.hold("a"):
            self.live()
            with other.hold("b", force=True):
                for operation in (
                    lambda: self.roster.begin(Spec("y", ("fake",)), sid=str(uuid4())),
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
                    with self.subTest(operation=operation), self.assertRaises(Stale):
                        operation()

    def test_only_current_holder_can_settle_receipts_during_reads(self):
        with self.roster.hold("owner") as lease:
            self.begin(note="keep")
            entry = self.roster.entries()["x"]
            receipt = Path(self.directory, entry.receipt)
            receipt.write_text(self.record.to_line() + "\n")
            data = self.content()
            data["entries"]["x"].update(
                record=json.loads(self.record.to_line()), state="live", argv=[]
            )
            Path(self.roster.path).write_text(json.dumps(data))
            before = Path(self.roster.path).read_bytes()
            for token in (None, Lease(lease.id, lease.generation + 1)):
                reader = Roster(self.directory)
                reader.bind(token)
                self.assertEqual(reader.entries()["x"].record, self.record)
                self.assertEqual(Path(self.roster.path).read_bytes(), before)
                self.assertTrue(receipt.exists())
            self.roster.entries()
            self.assertFalse(receipt.exists())
            self.assertIsNone(self.roster.entries()["x"].receipt)

    def test_bound_owner_requires_live_generation_and_release_clears_lease(self):
        self.roster.bind(Lease(str(uuid4()), 1))
        with self.assertRaises(Stale):
            self.begin()
        with self.roster.hold("a") as first:
            self.begin()
            self.roster.release()
            self.assertIsNone(self.content()["holder"])
            with self.assertRaises(Stale):
                self.roster.check_lease()
            with Roster(self.directory).hold("b") as second:
                self.assertGreater(second.generation, first.generation)
        self.assertIsNone(self.content()["holder"])

    def test_no_owner_retains_unleased_operations_only_when_holder_lock_free(self):
        self.live()
        self.roster.check_lease()
        self.roster.release()
        self.assertIsNone(self.content()["holder"])
        with Roster(self.directory).hold("owner"), self.assertRaises(Held):
            self.roster.note("x", "forbidden")

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
        self.assertEqual(
            sorted(os.listdir(self.directory)), ["holder.lock", "roster.json", "roster.lock"]
        )

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

    def test_sid_and_private_receipt_exist_before_start_and_cleanup_after_commit(self):
        self.begin("alias", "keep")
        entry = self.roster.entries()["alias"]
        self.assertEqual(entry.sid, self.record.sid)
        receipt = Path(self.directory, entry.receipt)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        with self.roster.open_receipt("alias") as output:
            output.write(b"diagnostic\n" + self.record.to_line().encode() + b"\n")
        self.assertEqual(self.roster.receipt_record("alias"), self.record)
        self.roster.commit("alias", self.record)
        self.assertFalse(receipt.exists())
        self.assertIsNone(self.roster.entries()["alias"].receipt)

    def test_commit_rejects_null_different_and_previously_owned_sid(self):
        self.begin()
        before = Path(self.roster.path).read_bytes()
        for sid in (None, str(uuid4())):
            with self.subTest(sid=sid), self.assertRaisesRegex(RosterError, "Record.sid"):
                self.roster.commit("x", replace(self.record, sid=sid))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        with self.assertRaisesRegex(RosterError, "already owned"):
            self.roster.begin(Spec("second", ("fake",)), sid=self.record.sid)

    def test_lost_adoption_matches_sid_not_name_or_sessions(self):
        self.begin("alias", "keep")
        self.roster.mark("alias", State.LOST)
        before = self.roster.entries()["alias"]
        record = replace(self.record, name="other", sessions=("unrelated",))
        self.assertEqual(self.roster.adopt(record), "alias")
        after = self.roster.entries()["alias"]
        self.assertEqual(
            (after.record, after.note, after.started), (record, before.note, before.started)
        )
        self.assertEqual(self.roster.adopt(record), "alias")
        self.assertEqual(list(self.roster.entries()), ["alias"])

    def test_unmatched_sid_leaves_a_lost_entry_unchanged(self):
        self.begin()
        self.roster.mark("x", State.LOST)
        before = Path(self.roster.path).read_bytes()
        self.assertIsNone(self.roster.adopt(replace(self.record, sid=str(uuid4()))))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)

    def test_ambiguous_or_already_owned_sid_is_refused_without_writes(self):
        self.begin("alias")
        self.roster.mark("alias", State.LOST)
        data = self.content()
        data["entries"]["duplicate"] = data["entries"]["alias"]
        Path(self.roster.path).write_text(json.dumps(data))
        before = Path(self.roster.path).read_bytes()
        with self.assertRaisesRegex(RosterError, "more than one"):
            self.roster.adopt(self.record)
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.roster.remove("duplicate")
        self.roster.adopt(self.record)
        with self.assertRaisesRegex(RosterError, "already owned"):
            self.roster.adopt(replace(self.record, ctl=("another-ctl",)))

    def test_adopted_record_retires_empty_or_mismatching_receipt(self):
        for contents in (
            b"",
            b"not a Record\n",
            replace(self.record, detail="older").to_line().encode(),
        ):
            with self.subTest(contents=contents):
                self.begin("alias")
                entry = self.roster.entries()["alias"]
                receipt = Path(self.directory, entry.receipt)
                receipt.write_bytes(contents)
                self.roster.mark("alias", State.LOST)
                self.roster.adopt(self.record)
                self.assertIsNone(self.roster.entries()["alias"].receipt)
                self.assertFalse(receipt.exists())
                self.roster.remove("alias")

    def test_adoption_keeps_active_stdout_and_original_record_read_available(self):
        self.begin("alias")
        entry = self.roster.entries()["alias"]
        receipt = Path(self.directory, entry.receipt)
        self.roster.mark("alias", State.LOST)
        with self.roster.open_receipt("alias") as output:
            self.roster.adopt(self.record)
            self.assertEqual(self.roster.entries()["alias"].receipt, entry.receipt)
            self.assertTrue(receipt.exists())
            contents = b"late child output\n" + self.record.to_line().encode() + b"\n"
            output.write(contents)
            output.flush()
            self.assertEqual(receipt.read_bytes(), contents)
            self.assertEqual(self.roster.receipt_record("alias"), self.record)
            self.roster.commit("alias", self.record)
            self.assertTrue(receipt.exists())
        self.roster.entries()
        self.assertFalse(receipt.exists())

    def test_concurrent_adoption_does_not_bypass_stdout_sid_validation(self):
        for sid in (None, str(uuid4())):
            with self.subTest(sid=sid):
                self.begin("alias")
                entry = self.roster.entries()["alias"]
                self.roster.mark("alias", State.LOST)
                wrong = replace(self.record, sid=sid)
                with self.roster.open_receipt("alias") as output:
                    self.roster.adopt(self.record)
                    self.assertEqual(self.roster.entries()["alias"].receipt, entry.receipt)
                    output.write(wrong.to_line().encode() + b"\n")
                    output.flush()
                    actual = self.roster.receipt_record("alias")
                    self.assertEqual(actual, wrong)
                    with self.assertRaisesRegex(RosterError, "Record.sid"):
                        self.roster.commit("alias", actual)
                    self.assertEqual(self.roster.entries()["alias"].record, self.record)
                self.roster.entries()
                self.assertIsNone(self.roster.entries()["alias"].receipt)
                self.roster.remove("alias")

    def test_missing_receipt_reference_is_repaired_and_entry_remains_removable(self):
        for state in (State.STARTING, State.LOST, State.LIVE, State.GONE):
            with self.subTest(state=state):
                self.begin()
                entry = self.roster.entries()["x"]
                receipt = Path(self.directory, entry.receipt)
                if state == State.LOST:
                    self.roster.mark("x", state)
                elif state in {State.LIVE, State.GONE}:
                    data = self.content()
                    data["entries"]["x"].update(
                        record=json.loads(self.record.to_line()), state=state, argv=[]
                    )
                    Path(self.roster.path).write_text(json.dumps(data))
                receipt.unlink()
                warning = io.StringIO()
                with mock.patch("sys.stderr", warning):
                    self.roster.entries()
                self.assertIn("missing receipt", warning.getvalue())
                self.assertIsNone(self.roster.entries()["x"].receipt)
                self.roster.remove("x")
                self.assertEqual(self.roster.entries(), {})

    def test_unsafe_orphan_receipts_warn_without_blocking_ledger_operations(self):
        self.roster.adopt(self.record)
        target = Path(self.home, "outside")
        target.write_text("keep")
        for kind in ("symlink", "fifo", "hardlink"):
            with self.subTest(kind=kind):
                orphan = Path(self.directory, "receipt-unsafe.stdout")
                if kind == "symlink":
                    orphan.symlink_to(target)
                elif kind == "fifo":
                    os.mkfifo(orphan)
                else:
                    os.link(target, orphan)
                warning = io.StringIO()
                with mock.patch("sys.stderr", warning):
                    self.roster.note("x", kind)
                    self.assertEqual(self.roster.entries()["x"].note, kind)
                self.assertTrue(orphan.exists())
                self.assertIn("left in place", warning.getvalue())
                self.assertEqual(target.read_text(), "keep")
                orphan.unlink()
        self.roster.remove("x")

    def test_unsafe_referenced_receipt_can_be_removed_without_touching_target(self):
        self.begin()
        entry = self.roster.entries()["x"]
        receipt = Path(self.directory, entry.receipt)
        target = Path(self.home, "outside")
        target.write_text("keep")
        receipt.unlink()
        receipt.symlink_to(target)
        warning = io.StringIO()
        with mock.patch("sys.stderr", warning):
            self.assertEqual(self.roster.entries()["x"].receipt, entry.receipt)
            self.roster.remove("x")
            self.assertEqual(self.roster.entries(), {})
        self.assertTrue(receipt.is_symlink())
        self.assertEqual(target.read_text(), "keep")
        self.assertIn("left in place", warning.getvalue())

    def test_receipt_survives_failure_until_remove_and_orphans_are_cleaned(self):
        self.begin()
        entry = self.roster.entries()["x"]
        receipt = Path(self.directory, entry.receipt)
        self.roster.mark("x", State.LOST)
        self.assertTrue(receipt.exists())
        orphan = Path(self.directory, "receipt-orphan.stdout")
        orphan.write_bytes(b"abandoned")
        self.roster.entries()
        self.assertFalse(orphan.exists())
        self.assertTrue(receipt.exists())
        self.roster.remove("x")
        self.assertFalse(receipt.exists())

    def test_receipt_symlink_and_fifo_are_refused(self):
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                self.begin()
                entry = self.roster.entries()["x"]
                path = Path(self.directory, entry.receipt)
                path.unlink()
                if kind == "symlink":
                    path.symlink_to(Path(self.home, "outside"))
                else:
                    os.mkfifo(path)
                with self.assertRaises((OSError, RosterError)), self.roster.open_receipt("x"):
                    self.fail("unsafe receipt opened")
                path.unlink()
                self.roster.remove("x")

    def test_holder_lock_is_private_noninheritable_and_force_fences_old_generation(self):
        with self.roster.hold("label") as old:
            path = Path(self.directory, "holder.lock")
            previous_inode = path.stat().st_ino
            fd = os.open(path, os.O_RDONLY)
            try:
                self.assertFalse(os.get_inheritable(fd))
                self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o600)
            finally:
                os.close(fd)
            other = Roster(self.directory)
            with other.hold("replacement", force=True) as new:
                self.assertGreater(new.generation, old.generation)
                self.assertNotEqual(path.stat().st_ino, previous_inode)
                self.assertEqual(self.content()["holder"]["id"], new.id)
                with self.assertRaises(Stale):
                    self.roster.check_lease()
            self.assertIsNone(self.content()["holder"])

    def test_holder_lock_symlink_fifo_and_other_lock_errors_refuse_even_force(self):
        self.roster.entries()
        path = Path(self.directory, "holder.lock")
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    path.symlink_to(Path(self.home, "outside"))
                else:
                    os.mkfifo(path)
                with self.assertRaises(RosterError), self.roster.hold("label", force=True):
                    self.fail("unsafe lock acquired")
                path.unlink()
        real_flock = fcntl.flock
        count = 0

        def flock(fd, flags):
            nonlocal count
            count += 1
            if count == 2:
                raise PermissionError("probe denied")
            return real_flock(fd, flags)

        with (
            mock.patch("fcntl.flock", side_effect=flock),
            self.assertRaisesRegex(RosterError, "probe denied"),
            self.roster.hold("label", force=True),
        ):
            self.fail("probe error became takeover")
        self.assertEqual(count, 2)
        self.assertIsNone(self.content()["holder"] if Path(self.roster.path).exists() else None)


if __name__ == "__main__":
    unittest.main()
