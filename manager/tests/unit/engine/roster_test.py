"""Roster state, lease, durable files and event cursors without live layer commands."""

import datetime
import fcntl
import io
import json
import os
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest import mock
from uuid import uuid4

from agent_pm.manager.engine.roster import Roster
from agent_pm.manager.engine.types import Held, Lease, RosterError, Spec, Stale, State
from agent_pm.utils import append_line

from manager.tests.case import with_ctl
from manager.tests.roster_case import RosterCase


class RosterTest(RosterCase):
    def test_missing_roster_reads_do_not_create_directories(self):
        self.assertEqual(self.roster.entries(), {})
        self.assertEqual(self.roster.backlog(), (0, 0, []))
        self.assertEqual(self.roster.next_event(0, 0), (0, None))
        self.assertEqual(self.roster.advance(0, 0), 0)
        self.assertEqual(self.roster.advance(None, 0), 0)
        self.roster.check_lease()
        self.assertFalse(self.roster.rotate_events())
        self.assertEqual(os.listdir(self.agent_pm), [])
        self.assertFalse(Path(self.directory).exists())

    def test_missing_entry_mutations_do_not_create_directories(self):
        for operation in (
            lambda: self.roster.note("missing", "note"),
            lambda: self.roster.remove("missing"),
            lambda: self.roster.mark("missing", State.GONE),
            lambda: self.roster.claim_resume("missing"),
            lambda: self.roster.commit("missing", self.record),
            lambda: self.roster.receipt_record("missing"),
        ):
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(RosterError, "not in roster"),
            ):
                operation()
            self.assertEqual(os.listdir(self.agent_pm), [])
        with (
            self.assertRaisesRegex(RosterError, "not in roster"),
            self.roster.open_receipt("missing"),
        ):
            self.fail("missing launch receipt opened")
        self.assertFalse(Path(self.directory).exists())

    def test_missing_roster_with_stale_lease_does_not_create_directories(self):
        self.roster.bind(Lease(str(uuid4()), 1))
        with self.assertRaises(Stale):
            self.roster.check_lease()
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_explicit_hold_and_events_can_create_missing_manager_directory(self):
        with self.roster.hold("owner"):
            self.assertTrue(Path(self.directory).is_dir())
        events_directory = str(Path(self.agent_pm, "managers", "events"))
        self.assertTrue(Path(Roster(events_directory).events).is_file())

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

    def test_persisted_invalid_sid_is_skipped_for_every_state(self):
        self.live()
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
                    warning = io.StringIO()
                    with mock.patch("sys.stderr", warning):
                        self.assertEqual(self.roster.entries(), {})
                    self.assertEqual(len(warning.getvalue().splitlines()), 1)
                    self.assertIn("canonical uuid4", warning.getvalue())
                    self.assertEqual(Path(self.roster.path).read_bytes(), before)
                    self.assertEqual(list(Path(self.directory).glob("receipt-*.stdout")), [])

    def test_resume_preflight_rejects_invalid_sid_without_receipt_or_write(self):
        self.live()
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
            warning = io.StringIO()
            with (
                mock.patch("sys.stderr", warning),
                self.assertRaisesRegex(RosterError, "not in roster"),
            ):
                operation()
            self.assertEqual(len(warning.getvalue().splitlines()), 1)
            self.assertIn("canonical uuid4", warning.getvalue())
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
                self.live(name, record=record)
                self.assertEqual(self.roster.adopt(record), name)
                self.assertEqual(Roster(self.directory).entries()[name].record, record)
        self.assertFalse((Path(self.directory).parent / "external").exists())

    def test_start_aliases_accept_printable_names_and_reject_controls(self):
        self.begin("alias / with spaces")
        self.assertIn("alias / with spaces", self.roster.entries())
        for name in ("", "bad\tname", "bad\nname"):
            with self.subTest(name=name), self.assertRaises(RosterError):
                self.begin(name)

    def test_adopt_takes_no_record_whose_sid_no_launch_holds(self):
        self.assertIsNone(self.roster.adopt(self.record))
        self.assertFalse(Path(self.directory).exists())
        self.live("alias")
        before = Path(self.roster.path).read_bytes()
        for record in (
            replace(self.record, sid=str(uuid4()), name="forged", sessions=("forged",)),
            replace(self.record, sid=str(uuid4()), name="alias", sessions=("alias",)),
            replace(self.record, sid=str(uuid4())),
        ):
            with self.subTest(record=record):
                self.assertIsNone(self.roster.adopt(record))
                self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(list(self.roster.entries()), ["alias"])

    def test_adopting_a_launched_record_is_idempotent(self):
        self.live("alias")
        before = Path(self.roster.path).read_bytes()
        self.assertEqual(self.roster.adopt(self.record), "alias")
        self.assertEqual(self.roster.adopt(self.record), "alias")
        self.assertEqual(Path(self.roster.path).read_bytes(), before)

    def test_note_can_be_replaced_or_cleared(self):
        self.live(note="first")
        self.roster.note("x", "second")
        self.assertEqual(self.roster.entries()["x"].note, "second")
        self.roster.note("x", None)
        self.assertIsNone(self.roster.entries()["x"].note)

    def test_mark_if_applies_only_to_matching_snapshot(self):
        self.live(note="keep")
        expected = self.roster.entries()["x"]
        self.assertTrue(self.roster.mark_if("x", expected, State.GONE))
        current = self.roster.entries()["x"]
        self.assertEqual(current, replace(expected, state=State.GONE))

    def test_mark_if_ignores_missing_or_changed_state(self):
        self.live()
        expected = self.roster.entries()["x"]
        self.roster.mark("x", State.DONE)
        before = Path(self.roster.path).read_bytes()
        self.assertFalse(self.roster.mark_if("x", expected, State.GONE))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.roster.remove("x")
        before = Path(self.roster.path).read_bytes()
        self.assertFalse(self.roster.mark_if("x", expected, State.GONE))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)

    def test_mark_if_ignores_replaced_sid_and_changed_note(self):
        self.live()
        expected = self.roster.entries()["x"]
        self.roster.note("x", "changed")
        before = Path(self.roster.path).read_bytes()
        self.assertFalse(self.roster.mark_if("x", expected, State.GONE))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.roster.remove("x")
        replacement = replace(self.record, sid=str(uuid4()))
        self.live(record=replacement)
        before = Path(self.roster.path).read_bytes()
        self.assertFalse(self.roster.mark_if("x", expected, State.GONE))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(self.roster.entries()["x"].record, replacement)

    def test_mark_if_checks_lease_before_missing_or_stale_snapshot(self):
        with self.roster.hold("first"):
            self.live()
            expected = self.roster.entries()["x"]
            with Roster(self.directory).hold("replacement", force=True):
                for name in ("x", "missing"):
                    with self.subTest(name=name), self.assertRaises(Stale):
                        self.roster.mark_if(name, expected, State.GONE)

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
            sorted(os.listdir(self.directory)),
            ["events.lock", "holder.lock", "roster.json", "roster.lock"],
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
            with self.subTest(raw=raw[:20]):
                if raw == valid.replace('"live"', '"impossible"'):
                    warning = io.StringIO()
                    with mock.patch("sys.stderr", warning):
                        self.assertEqual(self.roster.entries(), {})
                    self.assertEqual(len(warning.getvalue().splitlines()), 1)
                    self.assertIn("impossible", warning.getvalue())
                    self.assertEqual(Path(self.roster.path).read_text(), raw)
                    self.assertEqual(list(Path(self.directory).glob("receipt-*.stdout")), [])
                else:
                    with self.assertRaises(RosterError):
                        self.roster.entries()

    def test_invalid_entry_read_keeps_healthy_entry_and_original_ledger(self):
        self.live()
        self.live("bad", record=replace(self.record, name="bad", sid=str(uuid4())))
        data = self.content()
        data["entries"]["bad"]["note"] = "bad\nnote"
        Path(self.roster.path).write_text(json.dumps(data))
        before = Path(self.roster.path).read_bytes()
        warning = io.StringIO()
        with mock.patch("sys.stderr", warning):
            entries = self.roster.entries()
        self.assertEqual(list(entries), ["x"])
        self.assertEqual(entries["x"].record, self.record)
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn("entry 'bad'", warning.getvalue())

    def test_next_write_drops_invalid_entry_and_updates_healthy_entry(self):
        self.live()
        data = self.content()
        data["entries"]["bad"] = {"record": "invalid"}
        Path(self.roster.path).write_text(json.dumps(data))
        warning = io.StringIO()
        with mock.patch("sys.stderr", warning):
            self.roster.note("x", "updated")
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn("entry 'bad'", warning.getvalue())
        self.assertEqual(list(self.content()["entries"]), ["x"])
        self.assertEqual(self.roster.entries()["x"].note, "updated")
        self.roster.remove("x")
        self.assertEqual(self.roster.entries(), {})

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

    def test_busy_roster_lock_times_out_without_changing_ledger(self):
        self.live()
        before = Path(self.roster.path).read_bytes()
        fd = os.open(Path(self.directory, "roster.lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            ticks = 0

            def instant():
                nonlocal ticks
                ticks += 31
                return ticks

            with (
                mock.patch.object(time, "monotonic", side_effect=instant),
                self.assertRaisesRegex(RosterError, "busy"),
            ):
                self.roster.note("x", "blocked")
        finally:
            os.close(fd)
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertIsNone(self.roster.entries()["x"].note)

    def test_foreign_roster_lock_is_refused_without_changing_ledger(self):
        self.live()
        before = Path(self.roster.path).read_bytes()
        target = Path(self.directory, "roster.lock").stat()
        original = os.fstat

        def foreign(fd):
            opened = original(fd)
            if (opened.st_dev, opened.st_ino) == (target.st_dev, target.st_ino):
                values = list(opened)
                values[4] += 1
                return os.stat_result(values)
            return opened

        with (
            mock.patch.object(os, "fstat", side_effect=foreign),
            self.assertRaisesRegex(RosterError, "owned by you"),
        ):
            self.roster.note("x", "blocked")
        self.assertEqual(Path(self.roster.path).read_bytes(), before)

    def test_foreign_manager_directory_is_refused_without_changing_ledger(self):
        self.live()
        before = Path(self.roster.path).read_bytes()
        original = os.lstat

        def foreign(path, *args, **kwargs):
            opened = original(path, *args, **kwargs)
            if os.fspath(path) == self.directory:
                values = list(opened)
                values[4] += 1
                return os.stat_result(values)
            return opened

        with (
            mock.patch.object(os, "lstat", side_effect=foreign),
            self.assertRaisesRegex(RosterError, "directory owned by you"),
        ):
            self.roster.note("x", "blocked")
        self.assertEqual(Path(self.roster.path).read_bytes(), before)

    def test_rotate_refuses_unsafe_current_and_archive_files(self):
        path = Path(self.roster.events)
        data = b"h" * (1 << 20) + b"\n12:00:00 x done\n"
        path.write_bytes(data)
        self.roster.advance(1, 0)
        ledger = Path(self.roster.path).read_bytes()
        outside = Path(self.home, "outside")
        outside.write_bytes(b"keep")
        original = os.fstat
        original_stat = os.stat
        for target in (path, Path(str(path) + ".1")):
            for kind in ("symlink", "fifo", "foreign"):
                with self.subTest(target=target.name, kind=kind):
                    if target.exists():
                        target.unlink()
                    if kind == "symlink":
                        target.symlink_to(outside)
                    elif kind == "fifo":
                        os.mkfifo(target)
                    else:
                        target.write_bytes(data if target == path else b"archive")
                    identity = target.lstat()

                    def foreign(fd, kind=kind, identity=identity):
                        opened = original(fd)
                        if kind == "foreign" and (opened.st_dev, opened.st_ino) == (
                            identity.st_dev,
                            identity.st_ino,
                        ):
                            values = list(opened)
                            values[4] += 1
                            return os.stat_result(values)
                        return opened

                    def foreign_stat(file_path, *args, kind=kind, identity=identity, **kwargs):
                        opened = original_stat(file_path, *args, **kwargs)
                        if kind == "foreign" and (opened.st_dev, opened.st_ino) == (
                            identity.st_dev,
                            identity.st_ino,
                        ):
                            values = list(opened)
                            values[4] += 1
                            return os.stat_result(values)
                        return opened

                    with (
                        mock.patch.object(os, "fstat", side_effect=foreign),
                        mock.patch.object(os, "stat", side_effect=foreign_stat),
                        self.assertRaises(RosterError),
                    ):
                        self.roster.rotate_events()
                    self.assertEqual(Path(self.roster.path).read_bytes(), ledger)
                    self.assertEqual(outside.read_bytes(), b"keep")
                    target.unlink()
                    if target == path:
                        path.write_bytes(data)
        self.assertEqual(path.read_bytes(), data)

    def test_failed_archive_rename_preserves_events_cursor_and_generation(self):
        path = Path(self.roster.events)
        data = b"h" * (1 << 20) + b"\n12:00:00 x done\n"
        path.write_bytes(data)
        self.roster.advance(1, 0)
        ledger = Path(self.roster.path).read_bytes()
        original = os.replace

        def failed(source, destination):
            if os.fspath(source) == str(path) and os.fspath(destination) == str(path) + ".1":
                raise OSError("archive rename failed")
            return original(source, destination)

        with (
            mock.patch.object(os, "replace", side_effect=failed),
            self.assertRaisesRegex(RosterError, "archive rename failed"),
        ):
            self.roster.rotate_events()
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(Path(self.roster.path).read_bytes(), ledger)
        self.assertEqual((self.content()["cursor"], self.content()["gen"]), (1, 0))

    def test_created_and_rotated_events_file_mode_is_0600(self):
        path = Path(self.roster.events)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        path.write_bytes(b"h" * (1 << 20) + b"\n12:00:00 x done\n")
        self.roster.advance(1, 0)
        self.assertTrue(self.roster.rotate_events())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(str(path) + ".1").stat().st_mode), 0o600)

    def test_rotation_size_boundary_is_strict(self):
        path = Path(self.roster.events)
        path.write_bytes(b"h" * ((1 << 20) - 1) + b"\n")
        self.roster.advance(1, 0)
        ledger = Path(self.roster.path).read_bytes()
        self.assertFalse(self.roster.rotate_events())
        self.assertEqual(Path(self.roster.path).read_bytes(), ledger)
        append_line(path, b"12:00:00 x done\n")
        self.assertTrue(self.roster.rotate_events())
        self.assertEqual(self.roster.backlog(), (0, 1, ["12:00:00 x done"]))

    def test_rotation_consumed_byte_boundary_is_inclusive(self):
        path = Path(self.roster.events)
        half = (1 << 20) // 2
        path.write_bytes(b"h" * (half - 2) + b"\n" + b"u" * (half + 1) + b"\n")
        self.roster.advance(1, 0)
        self.assertFalse(self.roster.rotate_events())
        path.write_bytes(b"h" * (half - 1) + b"\n" + b"u" * half + b"\n")
        self.assertTrue(self.roster.rotate_events())
        self.assertEqual((self.content()["cursor"], self.content()["gen"]), (0, 1))

    def test_hostile_entry_diagnostic_is_one_escaped_line_with_healthy_entry_kept(self):
        self.live()
        data = self.content()
        hostile = "invalid\n\x1b[31mforged\u2028"
        data["entries"]["bad"] = {**data["entries"]["x"], "state": hostile}
        Path(self.roster.path).write_text(json.dumps(data))
        before = Path(self.roster.path).read_bytes()
        warning = io.StringIO()
        with mock.patch("sys.stderr", warning):
            entries = self.roster.entries()
        self.assertEqual(list(entries), ["x"])
        self.assertEqual(entries["x"].record, self.record)
        self.assertEqual(len(warning.getvalue().splitlines()), 1)
        self.assertIn(repr(hostile), warning.getvalue())
        self.assertNotIn("\x1b", warning.getvalue())
        self.assertNotIn("\u2028", warning.getvalue())
        self.assertEqual(Path(self.roster.path).read_bytes(), before)

    def test_top_level_faults_refuse_reads_and_writes_without_changing_bytes(self):
        self.live()
        valid = self.content()
        cases = []
        for field in valid:
            missing = {key: value for key, value in valid.items() if key != field}
            cases.append((f"missing {field}", missing))
        cases.append(("extra field", {**valid, "extra": 0}))
        for value in (True, 1, None, "2"):
            cases.append((f"version {value!r}", {**valid, "version": value}))
        for field in ("cursor", "gen", "lease_gen"):
            for value in (True, -1, None, "0", 1.5):
                cases.append((f"{field} {value!r}", {**valid, field: value}))
        for value in ([], None, "invalid"):
            cases.append((f"entries {value!r}", {**valid, "entries": value}))
        holder = {"id": str(uuid4()), "session": "owner"}
        for value in (
            False,
            [],
            "invalid",
            {},
            {"session": "owner"},
            {"id": holder["id"]},
            {**holder, "since": "now"},
            {**holder, "id": None},
            {**holder, "id": "invalid"},
            {**holder, "id": holder["id"].upper()},
            {**holder, "session": ""},
            {**holder, "session": "bad\nowner"},
            {**holder, "session": "bad\towner"},
        ):
            cases.append((f"holder {value!r}", {**valid, "holder": value, "lease_gen": 1}))
        cases.append(("holder with zero generation", {**valid, "holder": holder}))
        for label, data in cases:
            with self.subTest(label=label):
                raw = json.dumps(data).encode()
                Path(self.roster.path).write_bytes(raw)
                for operation in (self.roster.entries, lambda: self.roster.note("x", "blocked")):
                    with self.assertRaises(RosterError):
                        operation()
                    self.assertEqual(Path(self.roster.path).read_bytes(), raw)
                    self.assertEqual(list(Path(self.directory).glob("receipt-*.stdout")), [])

    def test_manager_directory_regular_file_is_refused_without_touching_it(self):
        path = Path(self.directory)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"keep")
        with self.assertRaises(RosterError):
            self.begin()
        self.assertEqual(path.read_bytes(), b"keep")
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_agent_pm_regular_file_is_refused_without_touching_it(self):
        path = Path(self.agent_pm)
        path.rmdir()
        path.write_bytes(b"keep")
        with self.assertRaises(RosterError):
            self.begin()
        self.assertEqual(path.read_bytes(), b"keep")
        self.assertFalse(Path(self.directory).exists())

    def test_roster_json_directory_is_refused_and_left_unchanged(self):
        self.live()
        path = Path(self.roster.path)
        path.unlink()
        path.mkdir()
        with self.assertRaises(RosterError):
            self.roster.entries()
        self.assertTrue(path.is_dir())
        self.assertEqual(list(path.iterdir()), [])

    def test_foreign_managers_parent_is_refused_without_mutation(self):
        self.live()
        before = Path(self.roster.path).read_bytes()
        parent = str(Path(self.directory).parent)
        original = os.lstat

        def foreign(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if os.fspath(path) == parent:
                values = list(info)
                values[4] += 1
                return os.stat_result(values)
            return info

        with (
            mock.patch.object(os, "lstat", side_effect=foreign),
            self.assertRaisesRegex(RosterError, "directory owned by you"),
        ):
            self.roster.note("x", "blocked")
        self.assertEqual(Path(self.roster.path).read_bytes(), before)

    def test_valid_roster_at_exact_size_cap_is_read_without_rewrite(self):
        self.live()
        raw = Path(self.roster.path).read_bytes()
        padded = raw + b" " * ((1 << 20) - len(raw))
        Path(self.roster.path).write_bytes(padded)
        self.assertEqual(self.roster.entries()["x"].record, self.record)
        self.assertEqual(Path(self.roster.path).read_bytes(), padded)

    def test_large_event_history_cursor_slices_across_read_blocks(self):
        path = Path(self.roster.events)
        lines = [f"12:00:00 worker done {index}" for index in range(20000)]
        path.write_text("\n".join(lines) + "\npartial")
        self.roster.advance(12345, 0)
        self.assertEqual(self.roster.backlog(), (12345, 0, lines[12345:]))
        self.assertEqual(self.roster.next_event(12345, 0), (12346, lines[12345]))
        self.roster.advance(len(lines) - 2, 0)
        self.assertEqual(self.roster.backlog(), (len(lines) - 2, 0, lines[-2:]))

    def test_new_directories_restore_owner_bits_stripped_by_umask(self):
        previous = os.umask(0o277)
        try:
            self.assertTrue(Path(self.roster.events).exists())
            for path in (Path(self.directory).parent, Path(self.directory)):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        finally:
            os.umask(previous)

    def test_existing_managers_parent_mode_is_preserved(self):
        parent = Path(self.directory).parent
        parent.mkdir(parents=True)
        parent.chmod(0o755)
        self.assertTrue(Path(self.roster.events).exists())
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(Path(self.directory).stat().st_mode), 0o700)

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

    def test_failed_rotation_refill_keeps_archive_and_rolls_back_generation(self):
        path = self.roster.events
        data = b"h" * (1 << 20) + b"\nunread\n"
        append_line(path, data)
        self.roster.advance(1, 0)
        ledger = Path(self.roster.path).read_bytes()
        original = os.replace

        def failed(source, destination):
            if os.fspath(destination) == path + ".next":
                raise OSError("disk full")
            return original(source, destination)

        with (
            mock.patch.object(os, "replace", side_effect=failed),
            self.assertRaises(RosterError) as failure,
        ):
            self.roster.rotate_events()
        self.assertEqual(Path(path + ".1").read_bytes(), data)
        self.assertEqual(Path(path).read_bytes(), data)
        self.assertEqual(Path(self.roster.path).read_bytes(), ledger)
        self.assertEqual(self.roster.backlog(), (1, 0, ["unread"]))
        self.assertIn(path + ".1", str(failure.exception))
        self.assertIn("after 1", str(failure.exception))

    def test_ledger_publish_failure_after_rotation_restores_event_bytes_and_numbering(self):
        path = self.roster.events
        data = b"h" * (1 << 20) + b"\nunread\n"
        append_line(path, data)
        self.roster.advance(1, 0)
        ledger = Path(self.roster.path).read_bytes()
        original = os.replace
        failed_once = False

        def failed(source, destination):
            nonlocal failed_once
            if os.fspath(destination) == self.roster.path and not failed_once:
                self.assertEqual(Path(path).read_bytes(), b"unread\n")
                failed_once = True
                raise OSError("ledger publish failed")
            return original(source, destination)

        with (
            mock.patch.object(os, "replace", side_effect=failed),
            self.assertRaisesRegex(RosterError, "ledger publish failed"),
        ):
            self.roster.rotate_events()
        self.assertTrue(failed_once)
        self.assertEqual(Path(path).read_bytes(), data)
        self.assertEqual(Path(path + ".1").read_bytes(), data)
        self.assertEqual(Path(self.roster.path).read_bytes(), ledger)
        self.assertEqual((self.content()["cursor"], self.content()["gen"]), (1, 0))
        self.assertEqual(self.roster.backlog(), (1, 0, ["unread"]))

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

    def test_receipt_record_ignores_blank_trailers_for_starting_and_resuming(self):
        for state in (State.STARTING, State.RESUMING):
            for trailer in (b"\n\n", b"\n \t\r\n\v\f\n", "\n\u2003\n".encode()):
                with self.subTest(state=state, trailer=trailer):
                    self._prepare_receipt(state)
                    with self.roster.open_receipt("x") as output:
                        output.write(b"diagnostic\xff\n" + self.record.to_line().encode() + trailer)
                    before = Path(self.roster.path).read_bytes()
                    self.assertEqual(self.roster.receipt_record("x"), self.record)
                    self.assertEqual(Path(self.roster.path).read_bytes(), before)
                    self.roster.commit("x", self.roster.receipt_record("x"))
                    entry = self.roster.entries()["x"]
                    self.assertEqual(
                        (entry.record, entry.state, entry.note), (self.record, State.LIVE, "keep")
                    )
                    self.assertIsNone(entry.receipt)
                    self.roster.remove("x")

    def test_receipt_record_refuses_empty_and_all_blank_starting_and_resuming(self):
        for state in (State.STARTING, State.RESUMING):
            for contents in (b"", b"\n \t\r\n\v\f\n", "\n\u2003\n".encode()):
                with self.subTest(state=state, contents=contents):
                    self._prepare_receipt(state)
                    with self.roster.open_receipt("x") as output:
                        output.write(contents)
                    before = Path(self.roster.path).read_bytes()
                    entry = self.roster.entries()["x"]
                    with self.assertRaisesRegex(RosterError, "no Record received"):
                        self.roster.receipt_record("x")
                    self.assertEqual(Path(self.roster.path).read_bytes(), before)
                    self.assertEqual(self.roster.entries()["x"], entry)
                    self.roster.remove("x")

    def test_receipt_record_refuses_a_malformed_last_nonblank_line(self):
        for state in (State.STARTING, State.RESUMING):
            for prefix in (b"", self.record.to_line().encode() + b"\n"):
                with self.subTest(state=state, prefix=prefix):
                    self._prepare_receipt(state)
                    with self.roster.open_receipt("x") as output:
                        output.write(prefix + b"not a Record\n \t\n")
                    before = Path(self.roster.path).read_bytes()
                    entry = self.roster.entries()["x"]
                    with self.assertRaisesRegex(RosterError, "invalid final Record"):
                        self.roster.receipt_record("x")
                    self.assertEqual(Path(self.roster.path).read_bytes(), before)
                    self.assertEqual(self.roster.entries()["x"], entry)
                    self.roster.remove("x")

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

    def test_adoption_ignores_another_sid_naming_a_live_control_name(self):
        self.live("alias", "keep")
        before = Path(self.roster.path).read_bytes()
        entries = self.roster.entries()
        self.assertIsNone(self.roster.adopt(replace(self.record, sid=str(uuid4()))))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(self.roster.entries(), entries)
        self.assertEqual(self.roster.adopt(self.record), "alias")
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(list(self.roster.entries()), ["alias"])

    def test_lost_adoption_refuses_another_alias_owning_the_control_name(self):
        self.live("owner", "owner note")
        sid = str(uuid4())
        self.roster.begin(Spec("lost alias", ("fake-ctl", "start"), "lost note"), sid=sid)
        self.roster.mark("lost alias", State.LOST)
        before = Path(self.roster.path).read_bytes()
        entries = self.roster.entries()
        with self.assertRaisesRegex(RosterError, "already owned by owner"):
            self.roster.adopt(replace(self.record, sid=sid))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(self.roster.entries(), entries)
        self.assertIs(self.roster.entries()["lost alias"].state, State.LOST)
        self.assertEqual(
            [entry.record for entry in self.roster.entries().values() if entry.record is not None],
            [self.record],
        )

    def test_commit_control_conflict_preserves_starting_intent_and_receipt(self):
        self.live("owner", "owner note")
        sid = str(uuid4())
        intent = self.roster.begin(
            Spec("starting alias", ("fake-ctl", "start"), "starting note"), sid=sid
        )
        if intent.receipt is None:
            self.fail("starting intent has no receipt")
        receipt = Path(self.directory, intent.receipt)
        before = Path(self.roster.path).read_bytes()
        entries = self.roster.entries()
        with self.assertRaisesRegex(RosterError, "already owned by owner"):
            self.roster.commit("starting alias", replace(self.record, sid=sid))
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(self.roster.entries(), entries)
        self.assertEqual(self.roster.entries()["starting alias"], intent)
        self.assertTrue(receipt.exists())
        self.assertEqual(receipt.read_bytes(), b"")
        self.assertEqual(
            [entry.record for entry in self.roster.entries().values() if entry.record is not None],
            [self.record],
        )

    def test_control_names_are_scoped_to_the_exact_ctl_command(self):
        self.live("owner")
        sid = str(uuid4())
        self.roster.begin(Spec("other ctl", ("agent-pm-delegate", "start")), sid=sid)
        other = with_ctl(replace(self.record, sid=sid), "agent-pm-delegate")
        self.roster.commit("other ctl", other)
        self.assertEqual(self.roster.adopt(other), "other ctl")
        distinct_name = replace(self.record, sid=str(uuid4()), name="different")
        self.live("different", record=distinct_name)
        self.assertEqual(self.roster.adopt(distinct_name), "different")
        self.assertEqual(
            {name: entry.record for name, entry in self.roster.entries().items()},
            {"owner": self.record, "other ctl": other, "different": distinct_name},
        )

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
            self.roster.adopt(with_ctl(self.record, "agent-pm-delegate"))

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
        self.live()
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
        Path(self.directory).mkdir(parents=True, mode=0o700)
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
