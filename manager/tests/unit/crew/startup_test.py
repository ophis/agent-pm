"""Startup behavior."""

import contextlib
import io
import os
import re
import stat
import time
import uuid
from unittest import mock

from agent_pm.manager.engine import crew, roster, workers
from agent_pm.session import (
    Access,
    Activity,
    Blocked,
    ConfigError,
    DriverError,
    Exited,
    IdleTimeout,
    SessionBound,
)
from agent_pm.session import Spec as SessionSpec

from manager.tests.unit.crew.case import CrewCase, WorkerCase
from manager.tests.unit.crew.data import DEAD_MSG, DEAD_TEXT, SID, UNPRINTABLE, start
from manager.tests.unit.crew.runtime import Api


class StartTest(WorkerCase):
    def test_a_worker_uses_the_common_spec_without_policy_flags(self):
        sid = self.started(prompt="do it", flags=("--model", "m"))
        self.assertEqual(str(uuid.UUID(sid)), sid)
        spec = SessionSpec(
            workdir=os.path.join(self.agent_pm, "workers", sid),
            sid=sid,
            name="w1",
            access=Access(cwd=self.dir, project=True),
            flags=("--model", "m"),
            env={},
            protect=self.protect(),
        )
        self.assertEqual(
            self.api.starts,
            [
                {
                    "runtime": "rt0",
                    "mode": "tui",
                    "prompt": "do it",
                    "spec": spec,
                    "resume": None,
                    "idle_timeout": workers.EARLY,
                    "name": "w1",
                    "sid": sid,
                    "events": self.events,
                }
            ],
        )
        self.assertEqual(spec.protect[:2], tuple(map(os.path.realpath, spec.protect[:2])))
        self.assertTrue(os.path.isfile(spec.protect[2]))

    def test_resume_passes_its_sid_as_resume(self):
        self.assertEqual(self.started(resume=SID, prompt=None), SID)
        (started,) = self.api.starts
        self.assertEqual((started["resume"], started["sid"], started["prompt"]), (SID, SID, ""))
        self.assertEqual(started["spec"].workdir, os.path.join(self.agent_pm, "workers", SID))

    def test_relative_cwd_made_absolute(self):
        old = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, old)
        sid = self.started(cwd=".")
        self.assertEqual(self.api.starts[0]["spec"].access.cwd, self.dir)
        self.assertEqual(self.record_of(sid)["cwd"], self.dir)

    def test_the_worker_record_private_in_private_dirs(self):
        sid = self.started(
            client="rt1",
            flags=("--x", "y z"),
            overrides=("A=1",),
            split="below",
            split_from="mgr",
            prompt="p",
        )
        want = self.record(
            sid,
            runtime="rt1",
            flags=["--x", "y z"],
            env={"A": "1"},
            split="below",
            split_from="mgr",
        )
        self.assertEqual(self.record_of(sid), want)
        workers = os.path.join(self.agent_pm, "workers")
        for path, mode in (
            (workers, 0o700),
            (os.path.join(workers, sid), 0o700),
            (os.path.join(workers, sid, "worker.json"), 0o600),
        ):
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), mode, path)
        self.assertEqual(os.listdir(os.path.join(workers, sid)), ["worker.json"])

    def test_a_failed_record_write_closes_the_session(self):
        path = os.path.join(roster.worker_dir(SID), "worker.json")
        os.mkdir(path)
        with self.assertRaisesRegex(crew.WorkersError, f"^{re.escape(path)}: .*; start undone$"):
            self.started(resume=SID)
        self.assertEqual([c[0] for c in self.api.calls], ["start", "close"])
        self.assertEqual(self.entries(), {})

    def test_a_bad_name_split_from_resume_note_or_env_starts_nothing(self):
        bad = "bad name 'a b': use [A-Za-z0-9_-]+"
        cases = (
            ({"name": "a b"}, bad),
            ({"split_from": "a b"}, bad),
            *(
                ({"resume": r}, f"--resume {r}: not a session id")
                for r in ("abc", SID.upper(), "", SID + "x", " " + SID, SID + "\n")
            ),
            ({"note": "x" * 501}, "note: 501 characters: want at most 500"),
            ({"note": "a\nb"}, f"note: 'a\\nb': {UNPRINTABLE}"),
            ({"overrides": ("HOME",)}, "--env HOME: want NAME=VALUE"),
            ({"overrides": ("=1",)}, "--env =1: want NAME=VALUE"),
            ({"overrides": ("A=a\nb",)}, f"--env A: 'a\\nb': {UNPRINTABLE}"),
        )
        for kw, msg in cases:
            with self.subTest(**kw):
                with self.assertRaises(crew.WorkersError) as cm:
                    self.started(**kw)
                self.assertEqual(str(cm.exception), msg)
                self.assertEqual(self.api.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.agent_pm, "workers")))

    def test_an_api_refusal_starts_nothing_more(self):
        for error in (
            ConfigError("--resume: picks the session; use crew.py start --resume <sid>"),
            ConfigError("env HOME: want one of A, B"),
            ConfigError("Codex tui writable root /w could rewrite hook dependency /w/x"),
            DriverError("tmux not found"),
        ):
            with self.subTest(str(error)):
                self.api = Api(own=None)
                self.api.start_error = error
                with self.assertRaises(crew.WorkersError) as cm:
                    self.started(resume=SID)
                self.assertEqual(str(cm.exception), str(error))
                self.assertEqual(self.api.calls, [("start", "w1")])
                self.assertEqual(os.listdir(os.path.join(self.agent_pm, "workers", SID)), [])
                self.assertEqual(self.entries(), {})

    def test_a_missing_directory_or_cwd_starts_nothing(self):
        nope = os.path.join(self.dir, "nope")
        for kw, msg in (
            (
                {"directory": os.path.join(self.dir, "m9")},
                f"manager directory {os.path.join(self.dir, 'm9')} not attached: "
                "run crew.py attach first",
            ),
            ({"cwd": nope}, f"--cwd {nope}: not a directory"),
        ):
            with self.subTest(msg):
                directory = kw.pop("directory", self.directory)
                with self.assertRaises(crew.WorkersError) as cm:
                    start("w1", directory, own=None, **{"cwd": self.dir, **kw})
                self.assertEqual(str(cm.exception), msg)
                self.assertEqual(self.api.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "m9")))

    def test_shown_with_its_layout_after_the_attach_line(self):
        for kw in (
            {},
            {"split_from": "x"},
            {"split": "below"},
            {"split_from": "x", "split": "right"},
        ):
            with self.subTest(**kw):
                self.api, err, seen = Api(own=None), io.StringIO(), []
                self.api.on_show = lambda _name, output=seen, error=err: output.append(
                    error.getvalue()
                )
                with contextlib.redirect_stderr(err):
                    self.started(**kw)
                self.assertEqual(self.api.shows, [("w1", kw.get("split"), kw.get("split_from"))])
                self.assertEqual(seen, ["tui: session w1: tmux attach -t '=w1'\n"])


class EarlyWatchTest(WorkerCase):
    RUNS = ("start", "restart")

    def watch(self, run, setup):
        if run == "restart":
            self.started(resume=SID)
        setup()
        return (
            self.started(resume=SID)
            if run == "start"
            else crew.restart("w1", own=None, driver=self.api)
        )

    def fails(self, run, setup):
        with self.assertRaises(crew.WorkersError) as cm:
            self.watch(run, setup)
        return str(cm.exception)

    def scripted(self, *events):
        return lambda: setattr(self.api, "events", list(events))

    def dies(self, text=DEAD_TEXT, code=1):
        def setup():
            self.api.events, self.api.dead = [Exited(code)], text

        return setup

    def test_a_running_worker_is_watched_until_its_idle_timeout(self):
        for run in self.RUNS:
            with self.subTest(run=run):
                self.api = Api(own=None)
                events = [Activity(), SessionBound("n"), IdleTimeout(5), Activity()]
                self.watch(run, self.scripted(*events))
                self.assertEqual(self.api.handle.taken, 3)

    def test_the_watch_ends_5_s_after_it_began(self):
        clock = [0.0]

        def ticking():
            for t in (1, 4.9, 5, 6):
                clock[0] = t
                yield Activity()

        with mock.patch.object(time, "monotonic", lambda: clock[0]):
            for run in self.RUNS:
                with self.subTest(run=run):
                    self.api, clock[0] = Api(own=None), 0.0
                    self.watch(run, lambda: setattr(self.api, "events", ticking))
                    self.assertEqual(clock[0], 5)

    def test_an_early_death_raises_its_report_keeping_the_session(self):
        kept = [f"kept {i}" for i in range(20)]
        lines = "".join(f"{x}\n\n  \n" for x in (*(f"old {i}" for i in range(7)), *kept))
        exited = "w1: rt0 exited 1 at once; its pane's last lines:\n"
        cases = (
            ("dead", self.dies(), DEAD_MSG),
            ("blank lines dropped, the last 20 kept", self.dies(lines), exited + "\n".join(kept)),
            ("fewer than 20 lines", self.dies("\n\none\n\n\ntwo\n\n"), exited + "one\ntwo"),
            (
                "an error above 40 blank rows",
                self.dies("usage: x\nx: error: bad\n" + "\n" * 41 + "Pane is dead (status 2)"),
                exited + "usage: x\nx: error: bad\nPane is dead (status 2)",
            ),
            ("a signal", self.dies(code=137), DEAD_MSG.replace("exited 1 ", "exited 137 ")),
            ("no last lines", self.dies(""), exited),
            (
                "gone",
                lambda: (self.dies()(), setattr(self.api, "gone", True)),
                "w1: session ended at once",
            ),
        )
        for why, setup, msg in cases:
            for run in self.RUNS:
                with self.subTest(why, run=run):
                    self.api = Api(own=None)
                    self.assertEqual(self.fails(run, setup), msg)
                    self.assertEqual(self.api.ran("close"), ["w1"] if run == "restart" else [])
                    self.assertEqual(self.api.calls[-1], ("sessions", "w1"))

    def test_a_dialog_ends_the_watch_on_its_stderr_line(self):
        for run in self.RUNS:
            with self.subTest(run=run):
                self.api, err = Api(own=None), io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.watch(run, self.scripted(Blocked("Trust alpha?"), Activity()))
                self.assertEqual(
                    err.getvalue().splitlines()[-1], "workers: w1: waits on a dialog: Trust alpha?"
                )
                self.assertEqual(self.api.handle.taken, 1)

    def test_a_failure_while_watching_raises(self):
        def unknown():
            self.api.error, self.api.error_after = DriverError("tmux sessions unknown: x"), 0
            yield Exited(1)

        for why, setup, msg in (
            (
                "events",
                self.scripted(DriverError("tui session w1: boom")),
                "tui session w1: boom",
            ),
            ("its row", lambda: setattr(self.api, "events", unknown), "tmux sessions unknown: x"),
        ):
            for run in self.RUNS:
                with self.subTest(why, run=run):
                    self.api = Api(own=None)
                    self.assertEqual(self.fails(run, setup), msg)


class RuntimeTest(WorkerCase):
    def test_the_client_else_the_own_rows_runtime_else_the_configs(self):
        for client, own_runtime, want in (
            (None, None, "rt0"),
            (None, "rt2", "rt2"),
            ("rt1", "rt2", "rt1"),
            ("rt1", None, "rt1"),
        ):
            with self.subTest(client=client, own_runtime=own_runtime):
                self.api = Api(own=None)
                sid = self.started(client=client, own_runtime=own_runtime)
                self.assertEqual(self.api.starts[0]["runtime"], want)
                self.assertEqual(self.record_of(sid)["runtime"], want)
                self.assertEqual(self.entries()["w1"]["resume"][8:10], ["--client", want])

    def test_a_bad_config_runtime_starts_nothing(self):
        for text in ('runtime = ""\n', "runtime = 1\n"):
            with self.subTest(text):
                self.local(text)
                with self.assertRaises(crew.WorkersError) as cm:
                    self.started()
                self.assertEqual(str(cm.exception), "config: runtime: want a runtime name")
                self.assertEqual(self.api.calls, [])

    def test_env_overrides_become_the_specs_env(self):
        sid = self.started(overrides=("B=", "A=0", "A=1"))
        self.assertEqual(dict(self.api.starts[0]["spec"].env), {"B": "", "A": "1"})
        self.assertEqual(self.record_of(sid)["env"], {"B": "", "A": "1"})


class ReplyTest(CrewCase):
    def test_a_bad_name_touches_nothing(self):
        with self.assertRaisesRegex(crew.WorkersError, r"^bad name 'a b': use \[A-Za-z0-9_-\]\+$"):
            crew.reply("a b", driver=self.api)
        self.assertEqual(self.api.calls, [])

    def test_its_tui_sessions_reply(self):
        for reply in ("done", "", None):
            with self.subTest(reply=reply):
                self.api.tuis["w1"] = (reply, "")
                self.assertEqual(crew.reply("w1", driver=self.api), reply)

    def test_no_tui_session(self):
        with self.assertRaisesRegex(crew.WorkersError, "^no tui session w1$"):
            crew.reply("w1", driver=self.api)
