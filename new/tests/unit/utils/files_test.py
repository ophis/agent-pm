"""new/core/src/utils/files.py: read_regular."""

import errno
import os
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

from agent_pm.utils import files

from new.tests.case import Case

DATA = b"0123456789"


class ReadRegular(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "f")

    def write(self, data=DATA, path=None):
        with open(path or self.path, "wb") as f:
            f.write(data)

    def promptly(self, call):
        """call()'s result; fails instead of hanging when it blocks."""
        out = {}
        thread = threading.Thread(target=lambda: out.setdefault("value", call()), daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "blocked")
        return out["value"]

    def test_a_regular_file_whole(self):
        self.write()
        self.assertEqual(files.read_regular(self.path), DATA)
        self.assertEqual(files.read_regular(Path(self.path)), DATA)
        self.write(b"")
        self.assertEqual(files.read_regular(self.path), b"")

    def test_limit_from_the_start_or_the_end(self):
        self.write()
        for limit, tail, want in (
            (4, False, b"0123"),
            (4, True, b"6789"),
            (0, False, b""),
            (0, True, b""),
            (20, False, DATA),
            (20, True, DATA),
            (None, True, DATA),
        ):
            with self.subTest(limit=limit, tail=tail):
                self.assertEqual(files.read_regular(self.path, limit=limit, tail=tail), want)

    def test_from_an_offset(self):
        self.write()
        for offset, limit, tail, want in (
            (3, None, False, b"3456789"),
            (3, 4, False, b"3456"),
            (3, 4, True, b"6789"),
            (8, 4, True, b"89"),
            (10, None, False, b""),
            (12, 4, False, b""),
        ):
            with self.subTest(offset=offset, limit=limit, tail=tail):
                self.assertEqual(
                    files.read_regular(self.path, offset=offset, limit=limit, tail=tail), want
                )

    def test_a_symlink_is_none(self):
        target = os.path.join(self.dir, "target")
        self.write(path=target)
        os.symlink(target, self.path)
        self.assertIsNone(files.read_regular(self.path))

    def test_a_fifo_or_directory_is_none_without_blocking(self):
        os.mkfifo(self.path)
        self.assertIsNone(self.promptly(lambda: files.read_regular(self.path)))
        self.assertIsNone(files.read_regular(self.dir))

    def test_no_fd_is_left_open(self):
        os.mkdir(self.path)
        before = len(os.listdir("/dev/fd"))
        for _ in range(3):
            self.assertIsNone(files.read_regular(self.path))
        self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_missing_is_none(self):
        self.assertIsNone(files.read_regular(self.path))
        self.assertIsNone(files.read_regular(os.path.join(self.path, "under-a-missing-dir")))

    def test_an_os_error_while_reading_is_none(self):
        self.write()
        with unittest.mock.patch.object(os, "fstat", side_effect=OSError(errno.EIO, "I/O error")):
            self.assertIsNone(files.read_regular(self.path))


if __name__ == "__main__":
    unittest.main()
