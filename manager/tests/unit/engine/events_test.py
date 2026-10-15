"""Event rotation transactions, filesystem validation and append ordering."""

import errno
import os
import stat
import threading
import time
from pathlib import Path
from unittest import mock

from agent_pm.utils import append_line, file_lock, save

from manager.tests.events_case import EventsCase


class EventsTest(EventsCase):
    def test_identity_returns_native_owned_file_identity_and_none_when_missing(self):
        opened = os.stat(self.events.path, follow_symlinks=False)
        self.assertEqual(self.events.identity(), (opened.st_dev, opened.st_ino))
        Path(self.events.path).unlink()
        self.assertIsNone(self.events.identity())

    def test_identity_refuses_symlink_fifo_and_directory(self):
        Path(self.events.path).unlink()
        outside = Path(self.directory, "outside")
        outside.write_bytes(b"keep\n")
        for kind in ("symlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    Path(self.events.path).symlink_to(outside)
                elif kind == "fifo":
                    os.mkfifo(self.events.path)
                else:
                    os.mkdir(self.events.path)
                with self.assertRaises(OSError):
                    self.events.identity()
                self.assertEqual(outside.read_bytes(), b"keep\n")
                if kind == "directory":
                    os.rmdir(self.events.path)
                else:
                    Path(self.events.path).unlink()

    def test_identity_refuses_a_file_owned_by_another_user(self):
        with mock.patch("os.getuid", return_value=os.getuid() + 1), self.assertRaises(OSError):
            self.events.identity()
        self.assertEqual(Path(self.events.path).read_bytes(), self.original)

    def test_committed_rotation_keeps_unread_order_and_cleans_intent(self):
        with file_lock(self.events.path, own=True):
            self.assertTrue(self.events.rotate(1, 0))
            self.assertTrue(Path(self.events.journal).is_file())
            self.assertEqual(Path(self.events.path).read_bytes(), b"old\n")
            save(self.ledger, '{"gen":1,"cursor":0}\n', own=True, durable=True)
            self.events.finish(1)
        self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
        self.assertFalse(Path(self.events.journal).exists())
        self.assertFalse(Path(self.events.candidate).exists())

    def test_uncommitted_rotation_rolls_back_before_another_rotation(self):
        with file_lock(self.events.path, own=True):
            self.assertTrue(self.events.rotate(1, 0))
        append_line(self.events.path, b"new\n", own=True, locked=True)
        self.recover()
        self.assertEqual(Path(self.events.path).read_bytes(), self.original + b"new\n")
        self.assertEqual(self.generation(), 0)
        self.assertFalse(Path(self.events.journal).exists())
        self.recover()
        self.assertEqual(Path(self.events.path).read_bytes(), self.original + b"new\n")

    def test_refill_failure_propagates_and_old_generation_recovers_all_lines(self):
        real_replace = os.replace

        def replace(source, destination):
            if str(destination) == self.events.candidate:
                raise OSError(errno.ENOSPC, "disk full")
            return real_replace(source, destination)

        with file_lock(self.events.path, own=True):
            with mock.patch("os.replace", side_effect=replace), self.assertRaises(OSError):
                self.events.rotate(1, 0)
            self.assertEqual(self.generation(), 0)
            self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
            self.events.recover(0)
        self.assertEqual(Path(self.events.path).read_bytes(), self.original)
        self.assertFalse(Path(self.events.journal).exists())

    def test_rotation_reprivatizes_fresh_0644_events_and_preserves_both_tails(self):
        def create_fresh(_seconds):
            self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
            Path(self.events.path).unlink()
            Path(self.events.path).write_bytes(b"fresh\n")
            Path(self.events.path).chmod(0o644)
            self.assertEqual(stat.S_IMODE(os.stat(self.events.path).st_mode), 0o644)

        with file_lock(self.events.path, own=True), mock.patch("time.sleep", create_fresh):
            self.assertTrue(self.events.rotate(1, 0))
            save(self.ledger, '{"gen":1,"cursor":0}\n', own=True, durable=True)
            self.events.finish(1)
        self.assertEqual(Path(self.events.path).read_bytes(), b"old\nfresh\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.events.path).st_mode), 0o600)
        self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
        self.assertEqual(self.generation(), 1)
        self.assertFalse(Path(self.events.journal).exists())

    def test_hostile_events_during_settlement_refuse_without_losing_unread_archive(self):
        outside = Path(self.directory, "outside")
        outside.write_bytes(b"keep\n")
        ledger = self.ledger.read_bytes()
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):

                def plant(_seconds, kind=kind):
                    self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
                    Path(self.events.path).unlink()
                    if kind == "symlink":
                        Path(self.events.path).symlink_to(outside)
                    else:
                        os.mkfifo(self.events.path)

                with file_lock(self.events.path, own=True):
                    with mock.patch("time.sleep", plant), self.assertRaises(OSError):
                        self.events.rotate(1, 0)
                    self.assertEqual(self.ledger.read_bytes(), ledger)
                    self.assertEqual(self.generation(), 0)
                    self.assertEqual(Path(self.events.archive).read_bytes(), self.original)
                    self.assertEqual(outside.read_bytes(), b"keep\n")
                    mode = os.stat(self.events.path, follow_symlinks=False).st_mode
                    self.assertTrue(
                        stat.S_ISLNK(mode) if kind == "symlink" else stat.S_ISFIFO(mode)
                    )
                    self.assertTrue(Path(self.events.journal).exists())
                    self.assertFalse(Path(self.events.candidate).exists())
                    Path(self.events.path).unlink()
                    self.events.recover(0)
                self.assertEqual(Path(self.events.path).read_bytes(), self.original)

    def test_legacy_settlement_prepends_old_tail_before_fresh_arrivals(self):
        fd = os.open(self.events.path, os.O_WRONLY | os.O_APPEND)
        self.addCleanup(os.close, fd)
        completed = threading.Event()

        def append():
            deadline = time.monotonic() + 5
            while not os.path.exists(self.events.archive) and time.monotonic() < deadline:
                time.sleep(0.01)
            os.write(fd, b"inflight\n")
            append_line(self.events.path, b"fresh\n", own=True)
            completed.set()

        thread = threading.Thread(target=append)
        thread.start()
        with file_lock(self.events.path, own=True):
            self.assertTrue(self.events.rotate(1, 0))
            self.events.finish(1)
        thread.join(5)
        self.assertTrue(completed.is_set())
        self.assertEqual(Path(self.events.path).read_bytes(), b"old\ninflight\nfresh\n")

    def test_a_line_appended_while_the_candidate_publishes_is_kept(self):
        real_replace = os.replace

        def replace(source, destination):
            if str(source) == self.events.candidate and str(destination) == self.events.path:
                append_line(self.events.path, b"late\n", own=True)
            real_replace(source, destination)

        with file_lock(self.events.path, own=True), mock.patch("os.replace", side_effect=replace):
            self.assertTrue(self.events.rotate(1, 0))
            self.events.finish(1)
        self.assertEqual(Path(self.events.path).read_bytes(), b"old\nlate\n")

    def test_rollback_keeps_fresh_bytes_already_in_prepared_candidate(self):
        real_replace = os.replace

        def replace(source, destination):
            if str(source) == self.events.candidate and str(destination) == self.events.path:
                real_replace(source, destination)
                raise OSError(errno.EIO, "publish failed after rename")
            real_replace(source, destination)

        fd = os.open(self.events.path, os.O_WRONLY | os.O_APPEND)
        self.addCleanup(os.close, fd)
        finished = threading.Event()

        def append():
            deadline = time.monotonic() + 5
            while not os.path.exists(self.events.archive) and time.monotonic() < deadline:
                time.sleep(0.01)
            os.write(fd, b"inflight\n")
            append_line(self.events.path, b"captured\n", own=True)
            finished.set()

        thread = threading.Thread(target=append)
        thread.start()
        with (
            file_lock(self.events.path, own=True),
            mock.patch("os.replace", side_effect=replace),
            self.assertRaises(OSError),
        ):
            self.events.rotate(1, 0)
        thread.join(5)
        self.assertTrue(finished.is_set())
        append_line(self.events.path, b"after\n", own=True, locked=True)
        self.recover()
        self.assertEqual(
            Path(self.events.path).read_bytes(),
            self.original + b"inflight\ncaptured\nafter\n",
        )

    def test_invalid_generation_and_changed_candidate_prefix_refuse_recovery(self):
        with file_lock(self.events.path, own=True):
            self.assertTrue(self.events.rotate(1, 0))
            with self.assertRaises(OSError):
                self.events.recover(2)
            with open(self.events.path, "r+b") as current:
                current.write(b"bad")
            with self.assertRaises(OSError):
                self.events.recover(1)
        self.assertTrue(Path(self.events.journal).exists())

    def test_journal_symlink_and_fifo_are_refused_without_following_or_waiting(self):
        outside = Path(self.directory, "outside")
        outside.write_text("keep")
        Path(self.events.journal).symlink_to(outside)
        with file_lock(self.events.path, own=True), self.assertRaises(OSError):
            self.events.recover(0)
        self.assertEqual(outside.read_text(), "keep")
        Path(self.events.journal).unlink()
        os.mkfifo(self.events.journal)
        with file_lock(self.events.path, own=True), self.assertRaises(OSError):
            self.events.recover(0)
