"""session/records.py: run.jsonl writes, reads and attempt completeness."""

import errno
import io
import json
import os
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import redirect_stderr
from datetime import datetime

from agent_pm.session import records
from agent_pm.utils import files

from new.tests.case import Case

SID = "4f1c2b9e-0d6a-4c1e-9b7a-2e5f8a3d6c10"


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "run.jsonl")

    def content(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()

    def write(self, *lines):
        with open(self.path, "wb") as f:
            f.writelines(
                (line if isinstance(line, bytes) else json.dumps(line).encode()) + b"\n"
                for line in lines
            )

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


class Stamp(Case):
    def test_is_local_iso_8601_with_offset_to_the_millisecond(self):
        text = records.stamp()
        self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d$")
        self.assertIsNotNone(datetime.fromisoformat(text).utcoffset())


class RecordPath(Case):
    def test_is_run_jsonl_in_the_absolute_workdir(self):
        self.assertEqual(records.CHANNEL, "run.jsonl")
        self.assertEqual(records.path("w"), os.path.join(os.path.abspath("w"), "run.jsonl"))


class Note(Base):
    def test_writes_one_json_line_with_a_millisecond_ts_kind_and_fields(self):
        records.note(self.path, "session", sid=SID, cwd="/wörk", project=False)
        line = self.content().decode()
        self.assertTrue(line.endswith("\n") and line.count("\n") == 1)
        self.assertIn("/wörk", line)
        record = json.loads(line)
        self.assertRegex(record.pop("ts"), r"T\d\d:\d\d:\d\d\.\d{3}[+-]")
        self.assertEqual(record, {"kind": "session", "sid": SID, "cwd": "/wörk", "project": False})

    def test_durable_syncs_the_file_and_its_directory(self):
        with unittest.mock.patch.object(os, "fsync", wraps=os.fsync) as fsync:
            records.note(self.path, "client_session", sid=SID, client_id="n")
            self.assertEqual(fsync.call_count, 0)
            records.note(self.path, "client_session", durable=True, sid=SID, client_id="n")
            self.assertEqual(fsync.call_count, 2)
        self.assertEqual(len(records.read(self.path)), 2)

    def test_an_os_error_is_raised_or_with_best_effort_printed(self):
        missing = os.path.join(self.dir, "gone", "run.jsonl")
        with self.assertRaises(OSError):
            records.note(missing, "stderr", text="x")
        err = io.StringIO()
        with redirect_stderr(err):
            records.note(missing, "stderr", best_effort=True, text="x")
        self.assertRegex(err.getvalue(), r"^records: .*gone")


class AppendLine(Case):
    def test_is_the_utils_one(self):
        self.assertIs(records.append_line, files.append_line)


class Holds(Base):
    def test_only_a_regular_file_holding_exactly_the_text(self):
        self.assertFalse(records.holds(self.path, "x"))
        with open(self.path, "w") as f:
            f.write("héllo")
        self.assertTrue(records.holds(self.path, "héllo"))
        for text in ("héll", "héllo!", "", "other"):
            self.assertFalse(records.holds(self.path, text), text)

    def test_a_symlink_or_fifo_is_no_without_blocking(self):
        target = os.path.join(self.dir, "target")
        with open(target, "w") as f:
            f.write("x")
        os.symlink(target, self.path)
        self.assertFalse(records.holds(self.path, "x"))
        os.remove(self.path)
        os.mkfifo(self.path)
        self.assertFalse(self.promptly(lambda: records.holds(self.path, "")))


class Read(Base):
    def test_the_json_object_lines_in_order_invalid_ones_skipped(self):
        first, second = {"kind": "session", "n": 1}, {"kind": "result", "n": "é"}
        self.write(
            first,
            b"",
            b"not json",
            b"[]",
            b"3",
            b"\xff\xfe",
            b'{"kind": "session"',
            b"[" * 100_000,
            second,
        )
        self.assertEqual(records.read(self.path), [first, second])

    def test_missing_or_empty_is_empty(self):
        self.assertEqual(records.read(self.path), [])
        self.write()
        self.assertEqual(records.read(self.path), [])

    def test_a_symlink_fifo_or_directory_is_never_read(self):
        target = os.path.join(self.dir, "planted.jsonl")
        with open(target, "w") as f:
            f.write(json.dumps({"kind": "session"}) + "\n")
        os.symlink(target, self.path)
        self.assertEqual(records.read(self.path), [])
        os.remove(self.path)
        os.mkfifo(self.path)
        self.assertEqual(self.promptly(lambda: records.read(self.path)), [])
        self.assertEqual(records.read(self.dir), [])

    def test_an_os_error_while_reading_is_empty(self):
        self.write({"kind": "session"})
        with unittest.mock.patch.object(os, "fstat", side_effect=OSError(errno.EIO, "I/O error")):
            self.assertEqual(records.read(self.path), [])


class Session(Base):
    def test_the_latest_valid_session_record_of_the_sid_wins(self):
        first = {"ts": "t", "kind": "session", "sid": SID, "cwd": "/a", "project": False}
        latest = {**first, "cwd": "/b", "project": True}
        self.write(
            first,
            latest,
            b"not json",
            b"[]",
            b"\xff\xfe",
            b'{"kind": "session"',
            {**first, "kind": "input"},
            {**first, "sid": "other"},
            {**first, "cwd": "rel"},
            {**first, "cwd": "/x\x1b[2J"},
            {**first, "cwd": 3},
            {**first, "project": 1},
            {k: v for k, v in first.items() if k != "project"},
        )
        self.assertEqual(records.session(self.dir, SID), latest)

    def test_metadata_updates_only_the_matching_latest_session(self):
        first = {"kind": "session", "sid": SID, "cwd": "/a", "project": False}
        self.write(
            first,
            {"kind": "session_metadata", "sid": "other", "resume": "wrong"},
            {"kind": "session_metadata", "sid": SID, "transcript": "/t", "resume": "resume"},
        )
        self.assertEqual(
            records.session(self.dir, SID), {**first, "transcript": "/t", "resume": "resume"}
        )
        self.write(first, {"kind": "session_metadata", "sid": SID, "transcript": "/t"}, first)
        self.assertEqual(records.session(self.dir, SID), first)

    def test_none_without_a_valid_session_record(self):
        self.assertIsNone(records.session(self.dir, SID))
        self.write(b"", {"ts": "t", "kind": "session", "sid": SID, "cwd": "/a", "project": "yes"})
        self.assertIsNone(records.session(self.dir, SID))


class Complete(Base):
    SESSION = {"kind": "session", "sid": SID, "cwd": "/a", "project": False}
    RESULT = {"kind": "result", "outcome": {"status": "done"}}
    END = {"kind": "end", "sid": SID, "rc": 0}

    def test_a_result_after_the_last_session_record(self):
        needs = {"kind": "result", "outcome": {"status": "needs_input"}}
        for lines, want in (
            ((self.SESSION, needs, self.END, self.SESSION), False),
            ((self.SESSION, self.RESULT, self.END), True),
            ((self.SESSION,), False),
            ((self.RESULT, self.END), False),
        ):
            with self.subTest([r["kind"] for r in lines]):
                self.write(*lines)
                self.assertIs(records.complete(self.dir), want)

    def test_missing_or_empty_is_not_complete(self):
        self.assertIs(records.complete(self.dir), False)
        self.write()
        self.assertIs(records.complete(self.dir), False)


if __name__ == "__main__":
    unittest.main()
