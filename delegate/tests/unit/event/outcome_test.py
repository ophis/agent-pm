"""delegate/event/outcome.py: SCHEMA beside it; main's drive.validate cases with the same results
(FR-20) and read with deliverable_file (FR-19); main's save cases: unit/utils/files_test.py."""

import dataclasses
import json
import os
import shutil
import tempfile
import threading
import unittest
import unittest.mock

from agent_pm.delegate.event import outcome
from agent_pm.delegate.event.outcome import (
    SCHEMA,
    STATUSES,
    InvalidOutcome,
    Outcome,
    read,
    validate,
)

from delegate.tests.case import Case

DONE = {"status": "done", "title": "T", "summary": "S", "deliverable": "# Doc\n"}
FILE = {"status": "done", "title": "T", "summary": "S", "deliverable_file": "out.md"}


LOCAL = {"type": "local"}


def schema():
    with open(SCHEMA) as f:
        return json.load(f)


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.work = os.path.join(self.tmp, "work")

    def check(self, data, output=None):
        return validate(data, output or LOCAL, self.work)

    def fails(self, data, msg, output=None):
        with self.assertRaises(InvalidOutcome) as cm:
            self.check(data, output)
        self.assertIn(msg, str(cm.exception))

    def touch(self, *parts, text="x"):
        path = os.path.join(self.work, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def outside(self, name="secret.md", text="key"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path


class Schema(Case):
    def test_schema_and_validate_agree(self):
        s = schema()
        self.assertEqual(tuple(s["properties"]["status"]["enum"]), STATUSES)
        self.assertEqual(set(s["properties"]), {f.name for f in dataclasses.fields(Outcome)})
        self.assertEqual(s["properties"]["questions"]["maxItems"], 4)


class Validate(Base):
    def test_valid(self):
        self.assertEqual(
            self.check({**DONE, "title": "  T  "}), Outcome("done", "T", "S", deliverable="# Doc\n")
        )

    def test_the_shape_schema_and_title_hold(self):
        for data, msg in (
            ([DONE], "not an object"),
            ({**DONE, "status": "maybe"}, "status 'maybe'"),
            ({**DONE, "summary": 3}, "must be text"),
            ({**DONE, "deliverable": ["x"]}, "must be text"),
            ({**DONE, "files": "x.md"}, "must be lists"),
            ({**DONE, "extra": 1}, "unknown field 'extra'"),
            ({**DONE, "title": "x" * 201}, "title is longer than 200"),
            ({**DONE, "summary": "x" * 4001}, "summary is longer than 4000"),
            ({**DONE, "status": "needs_input", "questions": [3]}, "questions[0] must be text"),
            ({**DONE, "files": None}, "files must be a list"),
            ({**DONE, "url": None}, "url must be text"),
            *(
                ({**DONE, "title": title}, "printable")
                for title in ("", "  ", "a\nb", "a\x1b[2Jb", 5)
            ),
        ):
            with self.subTest(msg=msg, data=data):
                self.fails(data, msg)

    def test_questions(self):
        ask = {**DONE, "status": "needs_input"}
        self.fails(ask, "1–4 questions")
        self.fails({**ask, "questions": ["q"] * 5}, "1–4 questions")
        self.assertEqual(self.check({**ask, "questions": ["q?"]}).questions, ["q?"])
        self.assertEqual(self.check({**DONE, "questions": ["q"]}).questions, [])

    def test_done_needs_the_deliverable_when_it_comes_back(self):
        for output in ({"type": "local"}, {"type": "orchestrator"}):
            self.fails({**DONE, "deliverable": " "}, "must carry the deliverable", output)
        self.assertEqual(
            self.check({**DONE, "deliverable": ""}, {"type": "pull-request"}).deliverable, ""
        )
        self.assertEqual(
            self.check({**DONE, "status": "failed", "deliverable": ""}).status, "failed"
        )

    def test_url_must_fit_the_destination(self):
        gh = {"type": "github", "repo": "o/docs", "branch": "main"}
        ok = "https://github.com/o/docs/blob/main/R/x.md"
        self.assertEqual(self.check({**DONE, "url": ok}, gh).url, ok)
        for bad in (
            "https://evil.example/o/docs/blob/main/x.md",
            "https://github.com/o/docs/blob/dev/x.md",
        ):
            self.fails({**DONE, "url": bad}, "url must start with", gh)
        for bad in (
            ok + "/../../../../x/y",
            "https://github.com/o/docs/blob/main/%2e%2e/%2e%2e/x",
            ok + "\n)[x](y",
            ok + " x",
            "http://github.com/o/docs/blob/main/x.md",
            5,
        ):
            self.fails({**DONE, "url": bad}, "plain https link", gh)
        pr = {"type": "pull-request"}
        for good in (
            "https://github.com/o/n/pull/7",
            "https://ghe.io/o/n/compare/main...b?expand=1",
            "https://github.com/o/n/tree/b",
        ):
            self.assertEqual(self.check({**DONE, "url": good}, pr).url, good)
        self.fails(
            {**DONE, "url": "https://github.com/o/n/issues/1"}, "pull request, compare or tree", pr
        )
        self.assertEqual(self.check({**DONE, "url": "https://x"}).url, "")

    def test_files_are_single_link_md_files_under_the_workdir(self):
        plan = self.touch("docs", "plan.md")
        self.assertEqual(
            self.check({**DONE, "files": [plan, "docs/plan.md"]}).files,
            [os.path.realpath(plan)] * 2,
        )
        os.makedirs(self.work + "2")
        sibling = os.path.join(self.work + "2", "x.md")
        with open(sibling, "w") as f:
            f.write("x")
        outside = self.outside()
        os.symlink(outside, os.path.join(self.work, "link.md"))
        os.link(outside, os.path.join(self.work, "hard.md"))
        for bad in (
            self.touch("notes.txt"),
            os.path.join(self.work, "missing.md"),
            sibling,
            "link.md",
            "hard.md",
            os.path.join(self.work, "..", "secret.md"),
            os.path.expanduser("~/.ssh/id_rsa"),
            7,
            "a\0b.md",
        ):
            self.fails({**DONE, "files": [bad]}, "single-link .md file under the workdir")


class Read(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(self.work)

    def read(self, data, output=None):
        body = data if isinstance(data, str) else json.dumps(data)
        return read(body, output or LOCAL, self.work)

    def refused(self, data, msg):
        with self.assertRaises(InvalidOutcome) as cm:
            self.read(data)
        self.assertIn(msg, str(cm.exception))
        return str(cm.exception)

    def test_not_json(self):
        for body in ("", "{", "{'status': 'done'}", "[" * 10**6 + "]" * 10**6):
            with self.subTest(body=body[:20]):
                self.refused(body, "not JSON: ")

    def test_not_an_object(self):
        for body in ("[1]", '"x"', "3", "null"):
            with self.subTest(body=body):
                self.refused(body, "not an object")

    def test_without_deliverable_file_it_is_validate(self):
        self.assertEqual(self.read(DONE), self.check(DONE))
        self.refused({**DONE, "status": "maybe"}, "status 'maybe'")

    def test_deliverable_file_becomes_the_deliverable(self):
        text = "# Report\n\nü and more\n"
        path = self.touch("out.md", text=text)
        for name in ("out.md", path):
            with self.subTest(name=name):
                self.assertEqual(
                    self.read({**FILE, "deliverable_file": name}),
                    Outcome("done", "T", "S", deliverable=text),
                )

    def test_the_read_deliverable_is_validated(self):
        self.touch("out.md", text=" \n")
        self.refused(FILE, "must carry the deliverable")

    def test_deliverable_file_must_be_a_single_link_md_file_under_the_workdir(self):
        self.touch("notes.txt")
        outside = self.outside()
        os.symlink(outside, os.path.join(self.work, "link.md"))
        os.link(outside, os.path.join(self.work, "hard.md"))
        for name in (
            "missing.md",
            "notes.txt",
            "../secret.md",
            outside,
            "link.md",
            "hard.md",
            7,
            None,
            ["out.md"],
            "a\0b.md",
        ):
            with self.subTest(name=name):
                self.refused(
                    {**FILE, "deliverable_file": name}, "single-link .md file under the workdir"
                )

    def test_a_fifo_is_refused_at_once(self):
        os.mkfifo(os.path.join(self.work, "out.md"))
        failed = []
        worker = threading.Thread(
            target=lambda: failed.append(self.refused(FILE, "single-link .md file")), daemon=True
        )
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "read blocked on a FIFO")
        self.assertEqual(len(failed), 1)

    def test_a_file_over_the_cap_is_too_large(self):
        cap = 4 * schema()["properties"]["deliverable"]["maxLength"]
        with open(os.path.join(self.work, "out.md"), "wb") as f:
            f.truncate(cap + 1)
        self.refused(FILE, "too large")

    def test_a_file_not_utf8_is_refused(self):
        with open(os.path.join(self.work, "out.md"), "wb") as f:
            f.write(b"# Doc \xff\n")
        self.refused(FILE, "not UTF-8 text")

    def test_a_name_with_control_characters_is_shown_as_its_repr(self):
        name = "a\x1b[2J\nb.md"
        error = self.refused({**FILE, "deliverable_file": name}, repr(name))
        self.assertTrue(error.isprintable(), error)

    def test_deliverable_and_deliverable_file_together(self):
        self.touch("out.md")
        for deliverable in ("# Doc\n", ""):
            with self.subTest(deliverable=deliverable):
                self.refused(
                    {**FILE, "deliverable": deliverable},
                    "give deliverable or deliverable_file, not both",
                )


class ReadRace(Base):
    """A file swapped between _file's check and the open, faked by patching os.open to swap first."""

    def swapped(self, swap, reason=""):
        real_open = os.open
        done = []

        def open_(path, *args, **kw):
            if not done:
                done.append(swap())
            return real_open(path, *args, **kw)

        with unittest.mock.patch("os.open", side_effect=open_):
            with self.assertRaises(InvalidOutcome) as cm:
                read(json.dumps({**FILE, "deliverable_file": "d/out.md"}), LOCAL, self.work)
        self.assertEqual(len(done), 1)
        self.assertIn("deliverable_file ", str(cm.exception))
        self.assertIn(reason, str(cm.exception))

    def setUp(self):
        super().setUp()
        self.target = self.touch("d", "out.md", text="# Doc\n")
        self.secret = self.outside("secret.md")

    def test_a_symlink_to_an_outside_md_file(self):
        def swap():
            os.unlink(self.target)
            os.symlink(self.secret, self.target)

        self.swapped(swap)

    def test_a_fifo(self):
        def swap():
            os.unlink(self.target)
            os.mkfifo(self.target)

        self.swapped(swap, "not a single-link regular file")

    def test_a_hard_link_to_an_outside_md_file(self):
        def swap():
            os.unlink(self.target)
            os.link(self.secret, self.target)

        self.swapped(swap, "not a single-link regular file")

    def test_a_parent_dir_swapped_for_a_symlink_out(self):
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        shutil.copy(self.secret, os.path.join(out, "out.md"))

        def swap():
            os.rename(os.path.dirname(self.target), os.path.join(self.work, "d-old"))
            os.symlink(out, os.path.dirname(self.target))

        self.swapped(swap, "no longer under the workdir")

    def test_a_file_replaced_after_the_open(self):
        real_open = os.open
        other = self.touch("d", "other.md", text="other\n")

        def open_(path, *args, **kw):
            fd = real_open(path, *args, **kw)
            os.rename(self.target, os.path.join(self.work, "d", "kept.md"))
            os.rename(other, self.target)
            return fd

        with unittest.mock.patch("os.open", side_effect=open_):
            with self.assertRaises(InvalidOutcome) as cm:
                read(json.dumps({**FILE, "deliverable_file": "d/out.md"}), LOCAL, self.work)
        self.assertIn("replaced while it was opened", str(cm.exception))


class SchemaFile(Case):
    def test_is_beside_outcome_py(self):
        self.assertEqual(os.path.dirname(SCHEMA), os.path.dirname(outcome.__file__))


if __name__ == "__main__":
    unittest.main()
