"""Crew's installed command talks only to executable fakes."""

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from agent_pm.manager import Crew, Roster, Spec, State
from agent_pm.utils import Record, Status

from manager.tests import fake_session
from manager.tests.case import REPO, Case
from utils.tests.fakes import ctl


def _without_manager_identity():
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "AGENT_PM_MANAGER",
            "AGENT_PM_HOLDER_ID",
            "AGENT_PM_HOLDER_GENERATION",
        }
    }


class Commands(Case):
    def setUp(self):
        self.command = self.setup_ctl()
        self.sessions = os.path.join(self.home, "session-list")
        fake_session.install(self.bin_dir, self.sessions)
        self.crew_bin = os.path.join(REPO, "bin", "agent-pm-crew")
        self.directory = os.path.join(self.agent_pm, "managers", "mgr")

    def invoke(self, *argv, clean=False):
        env = dict(os.environ)
        if clean:
            env.pop("PYTHONPATH", None)
        return subprocess.run(
            (self.crew_bin, *argv),
            cwd=self.home,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    def succeeded(self, *argv):
        result = self.invoke(*argv)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_help_from_an_unrelated_directory_without_pythonpath(self):
        result = self.invoke("--help", clean=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {
            "hold",
            "run",
            "attach",
            "status",
            "resume",
            "stop",
            "stop-all",
            "forget",
            "note",
            "next-event",
            "release",
        }
        self.assertEqual(set(result.stdout.split("{")[1].split("}")[0].split(",")), expected)
        self.assertFalse(Path(self.directory).exists())

    def test_unknown_and_removed_commands_exit_two(self):
        for args in (
            ("unknown",),
            ("start",),
            ("attach", "--resume"),
            ("run", "r"),
            ("resume", "--all"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.invoke(*args).returncode, 2)

    def test_invalid_manager_names_from_flag_and_environment_write_nowhere(self):
        env = _without_manager_identity()
        outside = Path(self.home, "outside")
        before = {str(path.relative_to(self.home)) for path in Path(self.home).rglob("*")}
        invalid = (
            ".",
            "..",
            "../outside",
            "../../outside",
            str(outside),
            "a/b",
            "a\\b",
            "name space",
            "name\n",
        )
        with mock.patch.dict(os.environ, env, clear=True):
            for source in ("flag", "environment"):
                for name in invalid:
                    with self.subTest(source=source, name=name):
                        if source == "flag":
                            result = self.invoke("attach", f"--manager={name}")
                        else:
                            with mock.patch.dict(os.environ, {"AGENT_PM_MANAGER": name}):
                                result = self.invoke("attach")
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn("manager name", result.stderr)
                        self.assertEqual(result.stdout, "")
                        after = {
                            str(path.relative_to(self.home)) for path in Path(self.home).rglob("*")
                        }
                        self.assertEqual(after, before)
                        self.assertFalse(Path(self.agent_pm, "managers").exists())
                        self.assertFalse(outside.exists())

    def test_missing_manager_argument_and_environment_exit_two_without_writes(self):
        env = _without_manager_identity()
        with mock.patch.dict(os.environ, env, clear=True):
            result = self.invoke("attach")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--manager NAME", result.stderr)
        self.assertIn("AGENT_PM_MANAGER", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(Path(self.agent_pm, "managers").exists())

    def test_valid_manager_names_work_from_flag_and_environment(self):
        env = _without_manager_identity()
        names = ("a", "A_9-z", "_leading", "9", "-dash", "trailing-")
        with mock.patch.dict(os.environ, env, clear=True):
            for source in ("flag", "environment"):
                for name in names:
                    with self.subTest(source=source, name=name):
                        alias = f"sample-{source}"
                        command = ("--", self.command, "start", f"{source}-{name}")
                        if source == "flag":
                            result = self.invoke("run", alias, f"--manager={name}", *command)
                        else:
                            with mock.patch.dict(os.environ, {"AGENT_PM_MANAGER": name}):
                                result = self.invoke("run", alias, *command)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        path = Path(self.agent_pm, "managers", name, "roster.json")
                        self.assertTrue(path.is_file())
                        record = Record.parse(result.stdout)
                        self.assertEqual(
                            json.loads(path.read_text())["entries"][alias]["record"],
                            json.loads(record.to_line()),
                        )

    def test_run_status_resume_and_stop_with_executable_fake(self):
        self.succeeded("attach", "--manager", "mgr")
        raw = self.succeeded(
            "run",
            "r",
            "--manager",
            "mgr",
            "--note",
            "kept",
            "--",
            self.command,
            "start",
            "worker",
            "--display",
            "opaque-value",
        )
        record = Record.parse(raw)
        roster = Roster(self.directory)
        self.assertEqual(roster.entries()["r"].record, record)
        self.assertEqual(roster.entries()["r"].note, "kept")
        self.assertEqual(record.display, "opaque-value")
        ctl.set_session(self.ctl_dir, "worker", alive=False)
        attached = self.succeeded("attach", "--manager", "mgr")
        self.assertIn("agent-pm-crew resume --manager mgr r", attached)
        self.assertEqual(roster.entries()["r"].state, State.GONE)
        resumed = Record.parse(self.succeeded("resume", "r", "--manager", "mgr"))
        self.assertEqual(resumed.display, record.display)
        self.assertEqual(resumed.resume, record.resume)
        status = Status.parse(self.succeeded("status", "r", "--manager", "mgr"))
        self.assertEqual(status.name, "r")
        self.assertTrue(status.alive)
        self.succeeded("stop", "r", "--manager", "mgr")
        self.assertEqual(roster.entries(), {})

    def test_status_batches_two_keys_and_skips_show_for_invisible_hosts(self):
        crew = Crew(Roster(self.directory))
        for name in ("a", "b"):
            crew.run(Spec(name, (self.command, "start", name)))
        self.succeeded("attach", "--manager", "mgr")
        lines = self.succeeded("status", "a", "b", "--manager", "mgr").splitlines()
        self.assertEqual([Status.parse(line).name for line in lines], ["a", "b"])
        calls = ctl.calls(self.ctl_dir)
        self.assertEqual(len([call for call in calls if call["argv"] == ["status"]]), 2)
        self.assertEqual([call for call in calls if call["argv"] == ["show"]], [])

    def test_stop_all_closes_roster_and_listed_orphans(self):
        crew = Crew(Roster(self.directory))
        crew.run(Spec("a", (self.command, "start", "a")))
        orphan = self.fake_record("orphan")
        ctl.set_session(self.ctl_dir, "orphan", alive=True)
        fake_session.set_records(self.sessions, [orphan])
        self.succeeded("stop-all", "--manager", "mgr")
        closed = [call["names"] for call in ctl.calls(self.ctl_dir) if call["argv"] == ["close"]]
        self.assertEqual(closed, [["a", "orphan"]])
        self.assertEqual(crew.roster.entries(), {})

    def test_orphans_are_adopted_once_and_release_supports_handoff(self):
        orphan = self.fake_record("orphan")
        ctl.set_session(self.ctl_dir, "orphan", alive=True)
        fake_session.set_records(self.sessions, [orphan])
        original = Roster(self.directory)
        with original.hold("first") as first:
            result = self.invoke("attach", "--manager", "mgr")
            self.assertEqual(result.returncode, 1)
            self.assertIn("held by first", result.stderr)
            with mock.patch.dict(
                os.environ,
                {
                    "AGENT_PM_HOLDER_ID": first.id,
                    "AGENT_PM_HOLDER_GENERATION": str(first.generation),
                },
            ):
                self.succeeded("attach", "--manager", "mgr")
                self.succeeded("release", "--manager", "mgr")
            with Roster(self.directory).hold("second") as second:
                with mock.patch.dict(
                    os.environ,
                    {
                        "AGENT_PM_HOLDER_ID": second.id,
                        "AGENT_PM_HOLDER_GENERATION": str(second.generation),
                    },
                ):
                    self.succeeded("attach", "--manager", "mgr")
                self.assertGreater(second.generation, first.generation)
                with mock.patch.dict(
                    os.environ,
                    {
                        "AGENT_PM_HOLDER_ID": first.id,
                        "AGENT_PM_HOLDER_GENERATION": str(first.generation),
                    },
                ):
                    result = self.invoke("note", "orphan", "stale write", "--manager", "mgr")
                self.assertEqual(result.returncode, 1)
                self.assertIn("stale holder generation", result.stderr)
        self.assertEqual(set(Roster(self.directory).entries()), {"orphan"})
        listing = Path(self.sessions, "calls.jsonl").read_text().splitlines()
        self.assertTrue(all(json.loads(line) == ["ls", "--here", "--records"] for line in listing))

    def test_note_cursor_and_next_event_only_print_event_lines(self):
        crew = Crew(Roster(self.directory))
        crew.run(Spec("a", (self.command, "start", "a")))
        self.succeeded("note", "a", "new", "--manager", "mgr")
        self.assertEqual(crew.roster.entries()["a"].note, "new")
        Path(crew.roster.events).write_text("12:00:00 a done\n")
        self.assertEqual(
            self.succeeded("next-event", "--manager", "mgr", "--after", "0", "--gen", "0"),
            "1 12:00:00 a done\n",
        )
        self.succeeded("attach", "--manager", "mgr", "--after", "1", "--gen", "0")
        self.assertEqual(crew.roster.backlog(), (1, 0, []))
