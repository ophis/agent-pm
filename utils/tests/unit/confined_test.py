"""Descriptor confinement, bounded reads, and pathname races."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_pm.utils import ConfinedFileError, ConfinedReason, read_confined

from utils.tests.case import Case


class ConfinedRead(Case):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "result.md"
        self.path.write_bytes(b"complete")

    def refused(self, reason, path=None):
        with self.assertRaises(ConfinedFileError) as error:
            read_confined(str(path or self.path), str(self.root), limit=8)
        self.assertIs(error.exception.reason, reason)
        return error.exception

    def test_exact_limit_and_empty_file(self):
        self.assertEqual(read_confined(str(self.path), str(self.root), limit=8), b"complete")
        self.path.write_bytes(b"")
        self.assertEqual(read_confined(str(self.path), str(self.root), limit=0), b"")

    def test_over_limit_reports_the_cap(self):
        self.path.write_bytes(b"too large")
        self.assertEqual(self.refused(ConfinedReason.TOO_LARGE).limit, 8)

    def test_symlink_and_missing_file_report_filesystem_failures(self):
        link = self.root / "link.md"
        link.symlink_to(self.path)
        for path in (link, self.root / "absent.md"):
            with self.subTest(path=path), self.assertRaises(OSError):
                read_confined(str(path), str(self.root), limit=8)

    def test_directory_and_fifo_are_refused(self):
        fifo = self.root / "fifo.md"
        os.mkfifo(fifo)
        for path in (self.root, fifo):
            with self.subTest(path=path):
                self.refused(ConfinedReason.REGULAR, path)

    def test_hard_link_is_refused(self):
        os.link(self.path, self.root / "linked.md")
        self.refused(ConfinedReason.SINGLE_LINK)

    def test_descriptor_suffix_is_checked(self):
        other = self.root / "result.txt"
        self.path.rename(other)
        self.refused(ConfinedReason.SUFFIX, other)
        self.assertEqual(
            read_confined(str(other), str(self.root), limit=8, suffix=".txt"), b"complete"
        )

    def test_outside_root_is_refused(self):
        child = self.root / "child"
        child.mkdir()
        with self.assertRaises(ConfinedFileError) as error:
            read_confined(str(self.path), str(child), limit=8)
        self.assertIs(error.exception.reason, ConfinedReason.OUTSIDE)

    def test_replacement_after_open_is_refused(self):
        original_open = os.open

        def replaced(path, *args, **kwargs):
            fd = original_open(path, *args, **kwargs)
            self.path.rename(self.root / "previous.md")
            self.path.write_bytes(b"changed")
            return fd

        with mock.patch.object(os, "open", side_effect=replaced):
            self.refused(ConfinedReason.REPLACED)

    def test_path_is_rechecked_after_read(self):
        original_stat = os.fstat
        seen = []

        def replaced(fd):
            seen.append(fd)
            if len(seen) == 2:
                self.path.rename(self.root / "previous.md")
                self.path.write_bytes(b"changed")
            return original_stat(fd)

        with mock.patch.object(os, "fstat", side_effect=replaced):
            self.refused(ConfinedReason.REPLACED)

    def test_ubuntu_descriptor_path_uses_proc(self):
        with (
            mock.patch.object(os, "readlink", return_value=str(self.path)) as readlink,
            mock.patch("sys.platform", "linux"),
        ):
            self.assertEqual(read_confined(str(self.path), str(self.root), limit=8), b"complete")
        self.assertEqual(readlink.call_count, 2)
        self.assertTrue(
            all(call.args[0].startswith("/proc/self/fd/") for call in readlink.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
