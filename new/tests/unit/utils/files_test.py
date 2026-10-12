"""new/core/src/utils/files.py: read_regular, append_line and save (main's save cases)."""

from new.tests import hermetic  # noqa: F401

import errno
import json
import os
import stat
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

from utils import files

DATA = b"0123456789"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "f")

    def content(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()

    def promptly(self, call):
        """call()'s result or exception; fails instead of hanging when it blocks."""
        out = {}

        def run():
            try:
                out["value"] = call()
            except BaseException as e:
                out["error"] = e

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(5)
        if thread.is_alive():
            self.fail("blocked")
        if "error" in out:
            raise out["error"]
        return out["value"]


class ReadRegular(Base):
    def write(self, data=DATA, path=None):
        with open(path or self.path, "wb") as f:
            f.write(data)

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


class AppendLine(Base):
    def test_appends_after_what_is_there_and_an_empty_text_creates_the_file_owner_only(self):
        files.append_line(self.path, "")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        files.append_line(self.path, "one\n")
        files.append_line(self.path, "two\n")
        self.assertEqual(self.content(), b"one\ntwo\n")

    def test_a_short_write_continues_with_the_rest(self):
        real = os.write
        with unittest.mock.patch.object(
            os, "write", side_effect=lambda fd, data: real(fd, bytes(data[:3]))
        ) as write:
            files.append_line(self.path, "héllo wörld\n")
        self.assertEqual((write.call_count, self.content()), (5, "héllo wörld\n".encode()))

    def test_a_symlink_is_refused(self):
        target = os.path.join(self.dir, "target")
        with open(target, "w") as f:
            f.write("keep\n")
        os.symlink(target, self.path)
        with self.assertRaises(OSError):
            files.append_line(self.path, "x\n")
        with open(target) as f:
            self.assertEqual(f.read(), "keep\n")

    def test_a_fifo_is_refused_without_blocking_with_or_without_a_reader(self):
        os.mkfifo(self.path)
        with self.assertRaises(OSError) as e:
            self.promptly(lambda: files.append_line(self.path, "x\n"))
        self.assertEqual(e.exception.errno, errno.ENXIO)
        reader = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, reader)
        with self.assertRaises(OSError) as e:
            self.promptly(lambda: files.append_line(self.path, "x\n"))
        self.assertEqual(e.exception.errno, errno.EINVAL)

    def test_a_lone_surrogate_is_written_as_a_replacement_character(self):
        files.append_line(self.path, json.dumps({"text": "a\ud800b"}, ensure_ascii=False) + "\n")
        self.assertEqual(self.content(), b'{"text": "a?b"}\n')


class Save(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, "sub", "f.txt")

    def test_writes_replaces_and_makes_the_directory(self):
        files.save(self.path, "one\n")
        files.save(self.path, "two\n")
        with open(self.path) as f:
            self.assertEqual(f.read(), "two\n")
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["f.txt"])

    def test_a_planted_symlink_is_replaced_not_followed(self):
        outside = os.path.join(self.dir, "outside")
        with open(outside, "w") as f:
            f.write("keep\n")
        os.makedirs(os.path.dirname(self.path))
        os.symlink(outside, self.path)
        files.save(self.path, "new\n")
        with open(self.path) as f, open(outside) as g:
            self.assertEqual(
                (os.path.islink(self.path), f.read(), g.read()), (False, "new\n", "keep\n")
            )

    def test_no_temp_is_left_when_the_replace_fails(self):
        files.save(self.path, "old\n")
        with unittest.mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                files.save(self.path, "new\n")
        with open(self.path) as f:
            self.assertEqual(
                (f.read(), os.listdir(os.path.dirname(self.path))), ("old\n", ["f.txt"])
            )


if __name__ == "__main__":
    unittest.main()
