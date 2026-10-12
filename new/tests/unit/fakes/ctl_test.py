"""tests/fakes/ctl.py: the installed fake-ctl, found on PATH, answers status, show and close for the Records on its
stdin, prints a Record for start and resume, and logs each call."""

import os
import subprocess
import unittest

from agent_pm.utils.record import Record, Status

from new.tests.case import Case
from new.tests.fakes import ctl


def record(name, display=None):
    resume = ("fake-ctl", "resume", name)
    return Record(1, "fake", name, (name,), None, None, ("fake-ctl",), resume, display, None)


def lines(*records):
    return "".join(r.to_line() + "\n" for r in records)


class FakeCtl(Case):
    def setUp(self):
        self.bin = os.path.join(self.home, "bin")
        self.state = os.path.join(self.home, "ctl state")
        self.ctl = ctl.install(self.bin, self.state)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        self.env["PATH"] = os.pathsep.join((self.bin, os.environ.get("PATH", os.defpath)))

    def call(self, *argv, stdin=""):
        return subprocess.run(
            argv, input=stdin, env=self.env, capture_output=True, text=True, timeout=30
        )

    def start(self, name, *argv):
        done = self.call(self.ctl, "start", name, *argv)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        return Record.parse(done.stdout)

    def status(self, *records):
        done = self.call(self.ctl, "status", stdin=lines(*records))
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        return [Status.parse(line) for line in done.stdout.splitlines()]

    def test_start_prints_one_record_whose_ctl_and_resume_name_fake_ctl(self):
        self.assertEqual(self.ctl, "fake-ctl")
        self.assertEqual(self.start("w1", "--display", "d"), record("w1", "d"))
        self.assertEqual(self.status(record("w1")), [Status("w1", True, "working", None, None)])

    def test_status_prints_one_line_per_record_and_an_unknown_name_is_gone(self):
        self.start("w1")
        self.assertEqual(
            self.status(record("w1"), record("w2")),
            [Status("w1", True, "working", None, None), Status("w2", False, "gone", None, None)],
        )
        ctl.set_session(self.state, "w1", alive=False, state="exited", outcome="needs_input")
        self.assertEqual(
            self.status(record("w1")), [Status("w1", False, "exited", "needs_input", None)]
        )

    def test_attach_is_a_command_only_while_alive_and_displayable(self):
        self.start("w1")
        self.assertIsNone(self.status(record("w1"))[0].attach)
        ctl.set_session(self.state, "w1", displayable=True)
        self.assertEqual(self.status(record("w1"))[0].attach, "fake-ctl attach w1")
        ctl.set_session(self.state, "w1", alive=False)
        self.assertIsNone(self.status(record("w1"))[0].attach)
        with self.assertRaises(TypeError):
            ctl.set_session(self.state, "w1", alvie=True)

    def test_show_exits_1_for_a_session_that_cannot_display(self):
        for name in ("w1", "w2"):
            self.start(name)
        ctl.set_session(self.state, "w2", displayable=True)
        shown = self.call(self.ctl, "show", stdin=lines(record("w2")))
        self.assertEqual((shown.returncode, shown.stdout, shown.stderr), (0, "", ""))
        refused = self.call(self.ctl, "show", stdin=lines(record("w1"), record("w2")))
        self.assertEqual(
            (refused.returncode, refused.stdout, refused.stderr),
            (1, "", "session w1: host fake cannot display\n"),
        )

    def test_close_ends_each_session(self):
        for name in ("w1", "w2"):
            self.start(name)
        done = self.call(self.ctl, "close", stdin=lines(record("w1"), record("w2")))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertEqual(
            self.status(record("w1"), record("w2")),
            [Status(name, False, "gone", None, None) for name in ("w1", "w2")],
        )

    def test_resume_of_a_live_session_prints_its_record_and_starts_nothing(self):
        started = self.start("w1", "--display", "d")
        ctl.set_session(self.state, "w1", state="blocked")
        again = self.call(*started.resume)
        self.assertEqual((again.returncode, again.stdout), (0, started.to_line() + "\n"))
        self.assertEqual(self.status(started), [Status("w1", True, "blocked", None, None)])

    def test_resume_of_an_ended_session_starts_it_with_its_display(self):
        started = self.start("w1", "--display", "d")
        ctl.set_session(self.state, "w1", alive=False, state="gone", outcome="failed")
        again = self.call(*started.resume)
        self.assertEqual((again.returncode, Record.parse(again.stdout)), (0, started))
        self.assertEqual(self.status(started), [Status("w1", True, "working", None, None)])

    def test_bad_stdin_a_bad_name_or_an_unknown_subcommand_exits_2_and_changes_nothing(self):
        self.start("w1")
        for argv, stdin in (
            (("status",), lines(record("w1")) + "not json\n"),
            (("close",), lines(record("w1")) + "[]\n"),
            (("show",), '{"v": 2}\n'),
            (("start", ""), ""),
            (("start", "a\nb"), ""),
            (("start",), ""),
            (("attach", "w1"), ""),
        ):
            with self.subTest(argv=argv, stdin=stdin):
                done = self.call(self.ctl, *argv, stdin=stdin)
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertTrue(done.stderr)
        self.assertEqual(self.status(record("w1")), [Status("w1", True, "working", None, None)])

    def test_stdin_too_deep_to_decode_exits_2(self):
        done = self.call(self.ctl, "status", stdin="[" * 200000 + "\n")
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertIn("not JSON", done.stderr)

    def test_calls_logs_each_call_with_its_argv_and_names(self):
        self.assertEqual(ctl.calls(self.state), [])
        self.start("w1", "--display", "d")
        self.status(record("w1"), record("w2"))
        self.call(self.ctl, "close", stdin="not json\n")
        self.assertEqual(
            ctl.calls(self.state),
            [
                {"argv": ["start", "w1", "--display", "d"], "names": ["w1"]},
                {"argv": ["status"], "names": ["w1", "w2"]},
                {"argv": ["close"], "names": []},
            ],
        )


if __name__ == "__main__":
    unittest.main()
