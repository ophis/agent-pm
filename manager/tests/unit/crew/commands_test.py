"""Commands behavior."""

import contextlib
import io
import os
import sys
from unittest import mock

from agent_pm.manager.engine import crew
from agent_pm.session import (
    ConfigError,
    DriverError,
    Exited,
)

from manager.tests.case import PYTHONPATH, SRC
from manager.tests.unit.crew.case import CrewCase, WorkerCase
from manager.tests.unit.crew.data import (
    DEAD_MSG,
    DEAD_TEXT,
    MODULE,
    SID,
    UNPRINTABLE,
    SessionView,
    row,
)
from manager.tests.unit.crew.runtime import Api


class MainTest(WorkerCase):
    def test_start(self):
        rc, out, err = self.run_main(
            "start",
            "w1",
            "--manager",
            "m1",
            "--cwd",
            self.dir,
            "--prompt",
            "go",
            "--note",
            "check it",
            "--",
            "--model",
            "sonnet",
            "--permission-mode",
            "default",
        )
        self.assertEqual(rc, 0, err)
        (started,) = self.api.starts
        sid = started["sid"]
        self.assertEqual(out, f"w1 {sid}\n")
        self.assertEqual(
            started["spec"].flags, ("--model", "sonnet", "--permission-mode", "default")
        )
        self.assertEqual((started["prompt"], started["events"]), ("go", self.events))
        e = self.entries()["w1"]
        self.assertEqual((e["kind"], e["sid"], e["note"]), ("worker", sid, "check it"))
        self.assertEqual(err, "tui: session w1: tmux attach -t '=w1'\n")

    def test_a_worker_starts_with_its_pythonpath_less_each_entry_resolving_to_its_import_root(self):
        root = SRC["session"]
        link = os.path.join(self.dir, "root")
        os.symlink(root, link)
        rel = os.path.relpath(root)
        cases = {
            "w1": (f"{PYTHONPATH}:/u1:{rel}:{link}:{root}/:/u2", "/u1:/u2"),
            "w2": (f"{rel}:{PYTHONPATH}", None),
            "w3": ("/u1", "/u1"),
            "w4": (f"{PYTHONPATH}:", None),
        }
        seen, fake = [], self.api.start

        def start(*args, **kw):
            seen.append(os.environ.get("PYTHONPATH"))
            return fake(*args, **kw)

        self.api.start = start
        for name, (given, want) in cases.items():
            with self.subTest(given=given), mock.patch.dict(os.environ, {"PYTHONPATH": given}):
                rc, _, err = self.run_main("start", name, "--manager", "m1", "--cwd", self.dir)
                self.assertEqual((rc, seen[-1:]), (0, [want]), err)

    def test_start_an_entry_write_failure_one_stderr_line_exit_0(self):
        flags = ["--append-system-prompt", "a\nb"]
        rc, out, err = self.run_main(
            "start", "w1", "--manager", "m1", "--cwd", self.dir, "--", *flags
        )
        sid = self.api.starts[0]["sid"]
        resume = [
            sys.executable,
            *MODULE,
            "start",
            "w1",
            "--manager",
            "m1",
            "--client",
            "rt0",
            "--resume",
            sid,
            "--cwd",
            self.dir,
            "--",
            *flags,
        ]
        self.assertEqual((rc, out), (0, f"w1 {sid}\n"))
        self.assertEqual(
            [x for x in err.splitlines() if not x.startswith("tui: ")],
            [
                f"manager: entry w1 not written: entry: resume: {resume!r}: "
                "want a list of 2 or more "
                "printable strings"
            ],
        )
        self.assertEqual(self.entries(), {})
        self.assertEqual(self.api.ran("close"), [])

    def test_start_client_env_and_split_in_the_entry(self):
        rc, _, err = self.run_main(
            "start",
            "w1",
            "--manager",
            "m1",
            "--cwd",
            self.dir,
            "--client",
            "rt1",
            "--env",
            "A=0",
            "--env",
            "B=",
            "--split",
            "below",
            "--",
            "--x",
        )
        self.assertEqual(rc, 0, err)
        (started,) = self.api.starts
        sid = started["sid"]
        self.assertEqual(
            (started["runtime"], dict(started["spec"].env)), ("rt1", {"A": "0", "B": ""})
        )
        self.assertEqual(
            self.entries()["w1"]["resume"],
            [
                sys.executable,
                *MODULE,
                "start",
                "w1",
                "--manager",
                "m1",
                "--client",
                "rt1",
                "--resume",
                sid,
                "--cwd",
                self.dir,
                "--env",
                "A=0",
                "--env",
                "B=",
                "--split",
                "below",
                "--",
                "--x",
            ],
        )

    def test_start_an_api_refusal_exits_1(self):
        for error in (
            ConfigError("env HOME: want one of A, B"),
            ConfigError("runtime 'nope' is not configured"),
            DriverError("tmux not found"),
        ):
            with self.subTest(str(error)):
                self.api = Api(own=None)
                self.api.start_error = error
                self.assertEqual(
                    self.run_main("start", "w1", "--manager", "m1", "--client", "nope"),
                    (1, "", f"workers: {error}\n"),
                )
                self.assertEqual(self.api.ran("show"), [])

    def test_start_resume(self):
        rc, out, err = self.run_main(
            "start",
            "w1",
            "--manager",
            "m1",
            "--cwd",
            self.dir,
            "--resume",
            SID,
            "--",
            "--model",
            "m",
        )
        self.assertEqual((rc, out), (0, f"w1 {SID}\n"), err)
        (started,) = self.api.starts
        self.assertEqual((started["resume"], started["spec"].flags), (SID, ("--model", "m")))

    def test_an_early_death_exits_1(self):
        for argv in (["start", "w1", "--manager", "m1", "--cwd", self.dir], ["restart", "w1"]):
            with self.subTest(argv[0]):
                self.api = Api(own=None)
                if argv[0] == "restart":
                    self.started(resume=SID)
                self.api.events, self.api.dead = [Exited(1)], DEAD_TEXT
                rc, out, err = self.run_main(*argv)
                self.assertEqual((rc, out), (1, ""))
                self.assertTrue(err.endswith(f"workers: {DEAD_MSG}\n"), err)
                self.assertEqual(err.count("workers:"), 1)

    def test_start_refusals_exit_1_starting_nothing(self):
        for argv, msg in (
            (["--resume", "abc"], "--resume abc: not a session id"),
            (["--note", "x" * 501], "note: 501 characters: want at most 500"),
            (["--note", "a\x07b"], f"note: 'a\\x07b': {UNPRINTABLE}"),
            (["--env", "HOME"], "--env HOME: want NAME=VALUE"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(
                    self.run_main("start", "w1", "--manager", "m1", *argv),
                    (1, "", f"workers: {msg}\n"),
                )
                self.assertEqual(self.api.ran("start"), [])

    def test_start_split_from_below(self):
        rc, _, err = self.run_main(
            "start",
            "w1",
            "--manager",
            "m1",
            "--cwd",
            self.dir,
            "--split-from",
            "x",
            "--split",
            "below",
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.api.shows, [("w1", "below", "x")])

    def test_start_defaults_cwd(self):
        rc, _, err = self.run_main("start", "w1", "--manager", "m1")
        self.assertEqual(rc, 0, err)
        record = self.record_of(self.api.starts[0]["sid"])
        self.assertEqual((record["cwd"], record["flags"]), (os.getcwd(), []))

    def test_restart(self):
        self.started(resume=SID)
        rc, out, err = self.run_main("restart", "w1")
        self.assertEqual((rc, out, err), (0, "", "tui: session w1: tmux attach -t '=w1'\n"))
        self.assertEqual(self.api.starts[-1]["resume"], SID)

    def test_main_uses_the_callers_session_driver(self):
        self.api.tuis["w1"] = ("done", "")
        self.assertEqual(self.run_main("reply", "w1"), (0, "done\n", ""))
        self.assertEqual(self.api.ran("attach"), ["w1"])

    def test_start_configures_the_session_before_starting_it(self):
        seen, start = [], self.api.start
        self.api.start = lambda *a, **kw: seen.append(self.api) or start(*a, **kw)
        rc, _, err = self.run_main("start", "w1", "--manager", "m1")
        self.assertEqual((rc, seen), (0, [self.api]), err)

    def test_a_config_error_exits_1(self):
        fake = self.api
        self.api = None
        for text in ("x = \n", "bogus = 1\n"):
            with self.subTest(text):
                self.local(text)
                rc, out, err = self.run_main("reply", "w1")
                self.assertEqual((rc, out), (1, ""))
                self.assertTrue(err.startswith("workers: config: "), err)
        self.assertEqual(fake.calls, [])

    def test_the_other_commands_work_with_a_bad_config(self):
        for text in ("x = \n", "bogus = 1\n"):
            self.local('runtime = "rt0"\n')
            self.started("w1", note="keep")
            self.started("w2")
            self.api.rows.pop("w2")
            self.local(text)
            workers = self.api
            for fake, argv in (
                (workers, ["note", "w1", "new", "--manager", "m1"]),
                (Api(), ["stop-all", "--dry-run"]),
                (workers, ["stop", "w1", "--manager", "m1"]),
                (workers, ["forget", "w2", "--manager", "m1"]),
                (workers, ["release", "--manager", "m1"]),
            ):
                with self.subTest(text=text, cmd=argv[0]):
                    rc, out, err = self.main_with(fake, *argv)
                    self.assertEqual(rc, 0, err)
                    self.assertNotIn("config", err)
            self.api = workers

    def test_reply(self):
        for reply, want in (("done", (0, "done\n", "")), ("", (0, "", "")), (None, (0, "", ""))):
            with self.subTest(reply=reply):
                self.api.tuis["w1"] = (reply, "")
                self.assertEqual(self.run_main("reply", "w1"), want)
        self.api.tuis.clear()
        self.assertEqual(self.run_main("reply", "w1"), (1, "", "workers: no tui session w1\n"))

    def test_unknown_sessions_exit_1(self):
        self.api.error = DriverError("tmux sessions unknown: tmux failed")
        for argv in (
            ["start", "w1", "--manager", "m1"],
            ["restart", "w1"],
            ["release", "--manager", "m1"],
        ):
            with self.subTest(argv[0]):
                self.assertEqual(
                    self.run_main(*argv), (1, "", "workers: tmux sessions unknown: tmux failed\n")
                )
        self.assertEqual(self.api.ran("start"), [])

    def test_usage_error(self):
        for argv in (["start"], ["start", "w1", "--manager", "m1", "--split", "left"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                crew.main(argv, driver=self.api)
            self.assertEqual(cm.exception.code, 2, argv)


class ManagerDirTest(CrewCase):
    CMDS = (["start", "w1"], ["next-event", "--after", "0", "--gen", "0"])

    def setUp(self):
        super().setUp()
        self.managers = os.path.join(self.agent_pm, "managers")
        self.nxt = self.enterContext(
            mock.patch.object(
                self.event_source, "next", return_value=(1, "10:00:01 w1 outcome done")
            )
        )

    def events_of(self, name):
        return os.path.join(self.managers, name, "events")

    def run_as(self, fake, argv):
        rc, _, err = self.main_with(fake, *argv)
        if argv[0] == "start":
            events = fake.starts[0]["events"] if fake.starts else None
        else:
            events = self.nxt.call_args.args[0] if self.nxt.called else None
        return rc, [x for x in err.splitlines() if not x.startswith("tui: ")], events

    def test_default_is_the_own_sessions_events(self):
        for cmd, session in ((self.CMDS[0], "m1"), (self.CMDS[1], "m2"), (["start", "w2"], "w1")):
            with self.subTest(cmd=cmd[0], session=session):
                self.attached(own=session)
                self.assertEqual(self.run_as(Api(session), cmd), (0, [], self.events_of(session)))

    def test_the_own_rows_runtime_is_the_default(self):
        self.attached(own="m1")
        for runtime, want in (("rt2", "rt2"), (None, "rt0")):
            with self.subTest(runtime=runtime):
                fake = Api("m1", rows=[row("m1", view=SessionView(runtime=runtime))])
                self.assertEqual(self.run_as(fake, self.CMDS[0])[0], 0)
                self.assertEqual(fake.starts[0]["runtime"], want)

    def test_manager_overrides_the_own_session(self):
        self.attached("--manager", "other", own="mgr")
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                self.assertEqual(
                    self.run_as(Api("mgr"), [cmd[0], "--manager", "other", *cmd[1:]]),
                    (0, [], self.events_of("other")),
                )
        self.assertEqual(os.listdir(self.managers), ["other"])

    def test_no_directory_or_a_bad_name_exit_1_making_nothing(self):
        outside = "workers: no manager directory: run inside tmux or give --manager <name>"
        bad = "workers: manager 'a/b': want [A-Za-z0-9_-]+"
        unknown = "tmux sessions unknown: bad $TMUX_PANE '%3;'"

        def failing():
            fake = Api("mgr")
            fake.error = DriverError(unknown)
            return fake

        cases = (
            *((cmd, lambda: Api(None), outside) for cmd in self.CMDS),
            *(
                ([cmd[0], "--manager", "a/b", *cmd[1:]], lambda: Api("mgr"), bad)
                for cmd in self.CMDS
            ),
            *((cmd, failing, f"workers: {unknown}; give --manager <name>") for cmd in self.CMDS),
        )
        for argv, make, msg in cases:
            with self.subTest(argv=argv, msg=msg):
                self.assertEqual(self.run_as(make(), argv), (1, [msg], None))
                self.assertEqual(self.made(), [])

    def test_events_option_is_gone_exit_2(self):
        for cmd in self.CMDS:
            with (
                self.subTest(cmd=cmd[0]),
                mock.patch.object(self.api, "start") as start,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as cm,
            ):
                crew.main(
                    [cmd[0], "--events", os.path.join(self.dir, "e"), *cmd[1:]], driver=self.api
                )
            self.assertEqual(cm.exception.code, 2)
            self.assertFalse(start.called or self.nxt.called)
            self.assertEqual(self.made(), [])

    def test_a_bad_directory_exit_1(self):
        real = os.path.join(self.dir, "managers")
        os.makedirs(os.path.join(real, "m1"), 0o700)
        os.symlink(real, self.managers)
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                self.assertEqual(
                    self.run_as(Api(None), [cmd[0], "--manager", "m1", *cmd[1:]]),
                    (
                        1,
                        [
                            f"workers: manager directory {self.managers}: "
                            "not a directory owned by you"
                        ],
                        None,
                    ),
                )
