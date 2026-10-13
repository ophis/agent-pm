"""utils/files.py: read_regular, append_line and save (main's save cases)."""

import errno
import json
import os
import stat
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path
from unittest import mock

from agent_pm.utils import ConfinedFileError, ConfinedReason, files, read_confined

from utils.tests.case import Case

DATA = b"0123456789"


class Base(Case):
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
            except (OSError, TypeError, ValueError) as e:
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

    def test_own_refuses_another_owners_file_and_writes_nothing(self):
        files.append_line(self.path, "keep\n")
        with (
            unittest.mock.patch.object(os, "getuid", return_value=os.getuid() + 1),
            self.assertRaises(OSError) as e,
        ):
            files.append_line(self.path, "x\n", own=True)
        self.assertEqual(
            (e.exception.errno, e.exception.strerror, e.exception.filename),
            (errno.EPERM, "not owned by you", self.path),
        )
        self.assertEqual(self.content(), b"keep\n")

    def test_own_appends_to_your_own_file(self):
        files.append_line(self.path, "one\n")
        files.append_line(self.path, "two\n", own=True)
        self.assertEqual(self.content(), b"one\ntwo\n")

    def test_by_default_the_owner_is_not_checked(self):
        files.append_line(self.path, "one\n")
        with unittest.mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            files.append_line(self.path, "two\n")
        self.assertEqual(self.content(), b"one\ntwo\n")

    def test_own_with_an_empty_text_creates_a_missing_file_owner_only(self):
        files.append_line(self.path, "", own=True)
        self.assertEqual((stat.S_IMODE(os.stat(self.path).st_mode), self.content()), (0o600, b""))


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
        with (
            unittest.mock.patch.object(os, "replace", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            files.save(self.path, "new\n")
        with open(self.path) as f:
            self.assertEqual(
                (f.read(), os.listdir(os.path.dirname(self.path))), ("old\n", ["f.txt"])
            )


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
