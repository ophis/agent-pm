"""agent_pm.manager.crew, the old core's crew_test ported onto a fake session.engine.api (Api): argv, env, tmux and the tui settings
are the Driver's now, and the worker record replaces the session options."""

import contextlib
import dataclasses
import datetime
import errno
import fcntl
import functools
import io
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from unittest import mock

from agent_pm.manager import crew, roster
from agent_pm.manager.engine.types import Spec
from agent_pm.session import (
    Access,
    Activity,
    Blocked,
    ConfigError,
    DriverError,
    Exited,
    IdleTimeout,
    SessionBound,
    SessionRow,
)
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import LOCAL

from manager.tests.case import PYTHONPATH, SRC, Case

CREW = os.path.join(SRC["manager"], "agent_pm", "manager", "crew.py")
MODULE = ["-P", "-m", "agent_pm.manager.crew"]
SID = "0b6f1c2e-3d4a-4b5c-8d6e-7f8091a2b3c4"
SID2 = "1" + SID[1:]
WHEN = "2026-10-09T12:00:00+00:00"
PY = "/usr/bin/python3"
SCRIPT = {"worker": "/x/crew.py", "role": "/x/drive.py", "pipeline": "/x/router.py"}
HEADER = "name\tkind\tsid\tstate\tnote\tcwd\tpane\n"
UNPRINTABLE = "want printable text (no control character, U+2028 or U+2029)"
DEAD_TEXT = "Error: boom\n\n  \n" + "\n" * 10 + "Pane is dead (status 1)\n"
DEAD_MSG = "w1: rt0 exited 1 at once; its pane's last lines:\nError: boom\nPane is dead (status 1)"
MGR = {"session": "mgr", "since": WHEN}
OTHER = {"session": "other", "since": WHEN}
LOCAL = os.path.basename(LOCAL)
WIDTH = 64
SPAN = roster.EVENTS_MAX // WIDTH


@contextlib.contextmanager
def fixed_time(text):
    instant = datetime.datetime.fromisoformat(text)
    with mock.patch.object(datetime, "datetime", wraps=datetime.datetime) as clock:
        clock.now.return_value = instant
        yield


def nl(*lines):
    return "".join(f"{x}\n" for x in lines)


def lock_free(path):
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


@contextlib.contextmanager
def within(seconds=10):
    def fire(*_):
        raise AssertionError("blocked")

    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def row(name, sid=None, state="working", pane=None, opener=None, shown=None, **kw):
    return SessionRow(
        name,
        sid,
        kw.get("runtime"),
        state,
        pane,
        opener,
        shown,
        kw.get("last_lines"),
        False,
    )


class Handle:
    def __init__(self, script):
        self.script, self.taken = script, 0

    def events(self):
        for event in self.script() if callable(self.script) else self.script:
            self.taken += 1
            if isinstance(event, Exception):
                raise event
            yield event


class Attached:
    def __init__(self, fake, name):
        self.fake, self.name = fake, name

    @property
    def reply(self):
        return self.fake.tuis[self.name][0]

    def screen(self, lines=None):
        self.fake.calls.append(("screen", self.name, lines))
        return self.fake.tuis[self.name][1]


class Api:
    """session.engine.api as crew calls it: rows by name (own: the caller's), start adding its row, every call recorded."""

    def __init__(self, own="mgr", rows=()):
        self.own, self.rows = own, {r.name: r for r in rows}
        if own is not None:
            self.rows.setdefault(own, row(own, shown="mgr"))
        self.calls, self.starts, self.shows = [], [], []
        self.error, self.error_after, self.start_error = None, 0, None
        self.events = [IdleTimeout(crew.EARLY)]
        self.dead = self.placed = self.on_show = self.handle = None
        self.gone, self.tuis, self.stuck = False, {}, set()

    def sessions(self, match=None):
        self.calls.append(("sessions", match))
        if self.error is not None and len(self.ran("sessions")) > self.error_after:
            raise self.error
        return [
            dataclasses.replace(r, own=n == self.own)
            for n, r in sorted(self.rows.items())
            if match is None or re.fullmatch(match, n)
        ]

    def start(self, runtime, mode, prompt, spec, resume=None, **kw):
        kw = {"name": spec.name, "sid": spec.sid, **kw}
        self.calls.append(("start", kw["name"]))
        self.starts.append(
            dict(runtime=runtime, mode=mode, prompt=prompt, spec=spec, resume=resume, **kw)
        )
        if self.start_error is not None:
            raise self.start_error
        if not self.gone:
            self.rows[kw["name"]] = row(
                kw["name"],
                resume or kw["sid"],
                "dead" if self.dead else "working",
                runtime=runtime,
                last_lines=self.dead,
            )
        self.handle = Handle(self.events)
        return self.handle

    def show(self, name, split=None, split_from=None, opener=None):
        self.calls.append(("show", name))
        self.shows.append((name, split, split_from))
        if self.on_show:
            self.on_show(name)
        if self.placed and name in self.rows:
            pane, opener = self.placed
            self.rows[name] = dataclasses.replace(self.rows[name], pane=pane, opener=opener)

    def attach_command(self, name):
        return f"tmux attach -t '={name}'"

    def close(self, name):
        self.calls.append(("close", name))
        if name in self.stuck:
            raise DriverError(f"cannot close tui session {name}: tmux: boom")
        self.rows.pop(name, None)

    def attach(self, name, idle_timeout=None):
        self.calls.append(("attach", name))
        if name not in self.tuis:
            raise DriverError(f"no tui session {name}")
        return Attached(self, name)

    def ran(self, what):
        return [c[1] if len(c) == 2 else c[1:] for c in self.calls if c[0] == what]


def start(name, directory, *, own, **kw):
    place = {k: kw.pop(k) for k in ("own_runtime", "split", "split_from", "driver") if k in kw}
    return crew.start(Spec(name, **kw), directory=directory, own=own, **place)


def started(name, *rest):
    """A worker entry's resume: crew run as a module."""
    return [PY, *MODULE, "start", name, *rest]


def recovered(argv):
    """A crew module's recovery as attach prints it."""
    return f"env PYTHONPATH={shlex.quote(PYTHONPATH)} {shlex.join(argv)}"


class Runs:
    """results by script, or by worker name for the crew module."""

    def __init__(self, results=None, lock=None):
        self.results, self.lock, self.recoveries = results or {}, lock, []

    @staticmethod
    def key(argv):
        return argv[5] if argv[1:4] == MODULE else argv[1]

    def __call__(self, argv, **kw):
        self.recoveries.append((argv, kw, self.lock is None or lock_free(self.lock)))
        result = self.results.get(self.key(argv), (0, ""))
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(argv, result[0], result[1])


class CrewCase(Case):
    """A caller-owned fake session driver, self.api (replaceable), sleep patched, stderr captured."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.api = Api(own=None)
        self.event_source = types.SimpleNamespace(next=crew.next_event)
        self.sleep = self.enterContext(mock.patch.object(time, "sleep"))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(mock.patch.dict(os.environ, {"PYTHONPATH": PYTHONPATH}))
        self.local('runtime = "rt0"\n')

    def call(self, fn, /, *args, **kw):
        return getattr(self.api, fn)(*args, **kw)

    def local(self, text):
        with open(os.path.join(self.agent_pm, LOCAL), "w") as f:
            f.write(text)

    def made(self):
        return sorted(set(os.listdir(self.agent_pm)) - {LOCAL})

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = crew.main(list(argv), driver=self.api, event_source=self.event_source)
        return rc, out.getvalue(), err.getvalue()

    def main_with(self, fake, *argv):
        self.api = fake
        return self.run_main(*argv)

    def attached(self, *argv, own=None):
        saved = self.api
        try:
            rc, _, err = self.main_with(Api(own), "attach", *argv)
        finally:
            self.api = saved
        if rc:
            raise AssertionError(f"attach exited {rc}: {err}")

    def record_of(self, sid):
        with open(os.path.join(self.agent_pm, "workers", sid, "worker.json")) as f:
            return json.load(f)


class WorkerCase(CrewCase):
    def setUp(self):
        super().setUp()
        self.directory = roster.directory("m1", driver=self.api)
        roster.ensure(self.directory)
        self.events = os.path.join(self.directory, "events")
        self.roster = os.path.join(self.directory, "roster.json")

    def started(self, name="w1", **kw):
        kw.setdefault("cwd", self.dir)
        return start(name, self.directory, own=None, driver=self.api, **kw)

    def entries(self):
        if not os.path.exists(self.roster):
            return {}
        with open(self.roster) as f:
            return json.load(f)["entries"]

    def protect(self):
        managers, workers = (os.path.join(self.agent_pm, d) for d in ("managers", "workers"))
        return tuple(map(os.path.realpath, (managers, workers, CREW)))

    def record(self, sid, **over):
        return {
            "version": 1,
            "name": "w1",
            "sid": sid,
            "runtime": "rt0",
            "cwd": self.dir,
            "flags": [],
            "env": {},
            "events": self.events,
            "manager": self.directory,
            "split": None,
            "split_from": None,
            **over,
        }


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
                    "idle_timeout": crew.EARLY,
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
                f"manager directory {os.path.join(self.dir, 'm9')} not attached: run crew.py attach first",
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
                self.api.on_show = lambda name: seen.append(err.getvalue())
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


class RestartTest(WorkerCase):
    def test_closes_then_starts_resuming_with_the_records_values(self):
        sid = self.started(
            client="rt1",
            flags=("--x", "y z"),
            overrides=("A=1",),
            split="below",
            split_from="mgr",
            note="keep",
            prompt="go",
        )
        entry, record = self.entries()["w1"], self.record_of(sid)
        self.local('runtime = "rt9"\n')
        self.api.calls.clear()
        self.assertIsNone(crew.restart("w1", own=None, driver=self.api))
        first, second = self.api.starts
        self.assertEqual(second, {**first, "prompt": "", "resume": sid})
        self.assertEqual(second["runtime"], "rt1")
        self.assertEqual(
            [c for c in self.api.calls if c[0] in ("close", "start", "show")],
            [("close", "w1"), ("start", "w1"), ("show", "w1")],
        )
        self.assertEqual(self.api.shows[-1], ("w1", "below", "mgr"))
        self.assertEqual(self.record_of(sid), record)
        self.assertEqual(self.entries()["w1"]["resume"], entry["resume"])
        self.assertEqual(self.entries()["w1"]["note"], "keep")

    def test_a_restarted_worker_sees_the_callers_pythonpath_without_the_import_root(self):
        self.started()
        seen, fake = [], self.api.start

        def start(*args, **kw):
            seen.append(os.environ.get("PYTHONPATH"))
            return fake(*args, **kw)

        self.api.start = start
        with mock.patch.dict(os.environ, {"PYTHONPATH": f"{PYTHONPATH}:/u1"}):
            self.assertEqual(self.run_main("restart", "w1")[0], 0)
        self.assertEqual(seen, ["/u1"])

    def test_a_bad_name_touches_nothing(self):
        with self.assertRaises(crew.WorkersError):
            crew.restart("a b", own=None, driver=self.api)
        self.assertEqual(self.api.calls, [])

    def assert_not_a_worker(self):
        with self.assertRaises(crew.NotAWorker) as cm:
            crew.restart("w1", own=None, driver=self.api)
        self.assertEqual(str(cm.exception), "w1: not a worker")
        self.assertEqual(self.api.ran("close"), [])
        self.assertEqual(len(self.api.starts), 1)

    def test_its_row_has_no_sid_not_a_worker(self):
        for why, rows in (
            ("no row", {}),
            ("no sid", {"w1": row("w1")}),
            ("not a session id", {"w1": row("w1", "x")}),
        ):
            with self.subTest(why):
                self.api = Api(own=None)
                self.started(name="w2", resume=SID)
                self.api.rows.update(rows)
                self.assert_not_a_worker()

    def tamper(self, write):
        self.api = Api(own=None)
        path = os.path.join(self.agent_pm, "workers", SID, "worker.json")
        if os.path.isdir(path) and not os.path.islink(path):
            os.rmdir(path)
        self.started(resume=SID)
        os.unlink(path)
        write(path)

    def test_a_bad_record_not_a_worker(self):
        def text(data):
            def write(path):
                with open(path, "w") as f:
                    f.write(data if isinstance(data, str) else json.dumps(data))

            return write

        good = self.record(SID)
        fields = {
            "version": (2, "1", True),
            "name": ("w2", None),
            "sid": (SID2, None),
            "runtime": ("", 1, "a\nb"),
            "cwd": ("rel", 1),
            "flags": ({}, [1], ["a\0b"]),
            "env": ([], {"A": 1}, {"A": "a\nb"}, {"": "x"}, {"A=B": "x"}),
            "events": ("rel", None),
            "manager": ("rel", "/x/a.b"),
            "split": ("left", 1),
            "split_from": ("a b", 1),
        }
        cases = {
            "missing": lambda path: None,
            "a symlink": lambda path: os.symlink(os.path.join(self.dir, "r.json"), path),
            "a fifo": os.mkfifo,
            "a directory": os.mkdir,
            "too big": text({**good, "flags": ["x" * (64 << 10)]}),
            "not JSON": text("{"),
            "a list": text([good]),
            "a key missing": text({k: v for k, v in good.items() if k != "env"}),
            "a key extra": text({**good, "x": 1}),
            **{f"{k} {v!r}": text({**good, k: v}) for k, values in fields.items() for v in values},
        }
        text(good)(os.path.join(self.dir, "r.json"))
        for why, write in cases.items():
            with self.subTest(why), within():
                self.tamper(write)
                self.assert_not_a_worker()

    def test_a_record_another_user_owns_not_a_worker(self):
        self.started(resume=SID)
        other = mock.Mock(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid() + 1, st_size=10)
        with mock.patch.object(os, "fstat", return_value=other):
            self.assert_not_a_worker()

    def test_a_record_the_spec_refuses_closes_nothing(self):
        self.tamper(lambda path: crew.save(path, json.dumps(self.record(SID, env={"A-B": "x"}))))
        with self.assertRaises(crew.WorkersError) as cm:
            crew.restart("w1", own=None, driver=self.api)
        self.assertEqual(str(cm.exception), "env must map variable names to strings without NUL")
        self.assertEqual((self.api.ran("close"), len(self.api.starts)), ([], 1))

    def test_a_failed_close_starts_nothing(self):
        self.started(resume=SID)
        self.api.stuck.add("w1")
        with self.assertRaisesRegex(crew.WorkersError, "^cannot close tui session w1: tmux: boom$"):
            crew.restart("w1", own=None, driver=self.api)
        self.assertEqual(len(self.api.starts), 1)

    def test_the_entry_re_recorded_best_effort(self):
        self.started(resume=SID)
        with roster.locked(self.directory) as r:
            r["holder"] = OTHER
        self.api.rows["other"] = row("other")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            crew.restart("w1", own=None, driver=self.api)
        self.assertIn(
            f"manager: entry w1 not written: manager directory {self.directory}: held by tmux session "
            f"other since {WHEN}; ask that manager to run crew.py release, or end that session\n",
            err.getvalue(),
        )
        self.assertEqual(len(self.api.starts), 2)


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
                f"manager: entry w1 not written: entry: resume: {resume!r}: want a list of 2 or more "
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
                fake = Api("m1", rows=[row("m1", runtime=runtime)])
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
                            f"workers: manager directory {self.managers}: not a directory owned by you"
                        ],
                        None,
                    ),
                )


class NextEventTest(CrewCase):
    A, B, C = "10:00:01 w1 done", "10:00:02 w2 blocked", "10:00:03 w1 outcome needs_input"

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, "events")

    def append(self, data, path=None):
        with open(path or self.path, "ab") as f:
            f.write(data if isinstance(data, bytes) else data.encode())

    def sleeping(self, *steps):
        calls = iter(steps)

        def sleep(secs):
            step = next(calls, None)
            if step is None:
                raise AssertionError("next_event kept waiting")
            step()

        return mock.patch.object(time, "sleep", side_effect=sleep)

    def error(self, reason, after=0, path=None):
        path = path or self.path
        with self.assertRaises(crew.WorkersError) as cm:
            crew.next_event(path, after)
        self.assertEqual(str(cm.exception), f"events file {path}: {reason}")

    def test_every_kind_is_an_event(self):
        kinds = [
            f"10:00:0{i} w1 {k}"
            for i, k in enumerate(("done", "blocked", "dead", "outcome failed", "new x y"))
        ]
        self.append(nl(*kinds))
        with self.sleeping():
            for n, event in enumerate(kinds):
                self.assertEqual(crew.next_event(self.path, n), (n + 1, event))

    def test_the_first_event_after_a_line_waited_for(self):
        A, B, C = self.A, self.B, self.C
        junk = [
            "",
            "garbage",
            "1:00:01 w1 done",
            "10:00:01 w1",
            "10:00:01 w1 ",
            "10:00:01  w1 done",
            " 10:00:01 w1 done",
        ]
        consumed = "0123456789abcdef\n"

        def rewrite():
            with open(self.path, "r+b") as f:
                f.write(b"\n" * len(consumed))

        cases = {
            "end skips what is there": (nl(A, B), [nl(C)], None, (3, C)),
            "end counts complete lines only": (
                nl(A) + "10:00:02 w1 do",
                ["ne\n"],
                None,
                (2, "10:00:02 w1 done"),
            ),
            "end of a missing file is zero": (None, [nl(A)], None, (1, A)),
            "appended while waiting": (nl(A), [nl(B)], 1, (2, B)),
            "fewer lines than after": (nl(A), [nl(B), nl(C)], 2, (3, C)),
            "a partial last line waits for its newline": (
                nl(A) + "10:00:02 w1 caf",
                [b"\xc3", b"\xa9\n"],
                1,
                (2, "10:00:02 w1 café"),
            ),
            "a missing file waited for": (None, [lambda: None, nl(A)], 0, (1, A)),
            "consumed bytes not read again": (consumed, [rewrite, nl(B)], 0, (2, B)),
            "malformed lines skipped but counted": (nl(A, *junk, B), [], 1, (len(junk) + 2, B)),
            "decoded as UTF-8 with replacement": (
                b"10:00:01 w1 caf\xc3\xa9 \xff\n",
                [],
                0,
                (1, "10:00:01 w1 café �"),
            ),
        }
        for why, (data, steps, after, want) in cases.items():
            with self.subTest(why):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
                if data is not None:
                    self.append(data)
                steps = [s if callable(s) else functools.partial(self.append, s) for s in steps]
                with self.sleeping(*steps) as sleep:
                    self.assertEqual(crew.next_event(self.path, after), want)
                self.assertEqual(sleep.call_args_list, [mock.call(0.5)] * len(steps))

    def test_a_shrunk_replaced_or_unreadable_file_stops_it(self):
        def shrink():
            with open(self.path, "wb") as f:
                f.write(nl(self.A).encode())

        def replace():
            with open(self.path + ".new", "wb") as f:
                f.write(nl(self.B, self.A).encode())
            os.replace(self.path + ".new", self.path)

        for reason, steps, after, path in (
            ("shrank", [shrink], 2, None),
            ("replaced", [replace], 2, None),
            (os.strerror(errno.EISDIR), [], 0, self.dir),
        ):
            with self.subTest(reason):
                with open(self.path, "wb") as f:
                    f.write(nl(self.A, self.B).encode())
                with self.sleeping(*steps):
                    self.error(reason, after, path)

    def test_main_prints_line_and_event(self):
        self.attached("--manager", "m1")
        self.api.rows.update(w1=row("w1", state="dead"), w2=row("w2", state="dead"))
        path = roster.events(roster.directory("m1", driver=self.api), create=False)
        self.append(nl(self.A, self.B), path)
        with self.sleeping():
            self.assertEqual(
                self.run_main("next-event", "--manager", "m1", "--after", "1", "--gen", "0"),
                (0, f"2 {self.B}\n", ""),
            )
        with self.sleeping(lambda: self.append(nl(self.C), path)):
            self.assertEqual(
                self.run_main("next-event", "--manager", "m1", "--after", "end", "--gen", "0"),
                (0, f"3 {self.C}\n", ""),
            )

    def test_main_with_a_bad_config(self):
        self.attached("--manager", "m1")
        self.api.rows.update(w1=row("w1", state="dead"), w2=row("w2", state="dead"))
        path = roster.events(roster.directory("m1", driver=self.api), create=False)
        self.append(nl(self.A, self.B), path)
        for text in ("x = \n", "bogus = 1\n"):
            self.local(text)
            with self.subTest(text), self.sleeping():
                self.assertEqual(
                    self.run_main("next-event", "--manager", "m1", "--after", "1", "--gen", "0"),
                    (0, f"2 {self.B}\n", ""),
                )

    def test_main_error_exit(self):
        self.attached("--manager", "m1")
        err = crew.WorkersError(f"events file {self.path}: {os.strerror(errno.EISDIR)}")
        with mock.patch.object(self.event_source, "next", side_effect=err):
            self.assertEqual(
                self.run_main("next-event", "--manager", "m1", "--after", "0", "--gen", "0"),
                (1, "", f"workers: {err}\n"),
            )

    def test_main_usage_error(self):
        for argv in (
            *(["--after", bad, "--gen", "0"] for bad in ("-1", "x", "1.5", "")),
            *(["--after", "1", "--gen", bad] for bad in ("-1", "x", "end", "")),
            ["--after", "1"],
            ["--gen", "0"],
            [],
        ):
            with (
                self.sleeping(),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as cm,
            ):
                crew.main(["next-event", "--manager", "m1", *argv], driver=self.api)
            self.assertEqual(cm.exception.code, 2, argv)


class NextEventContentTest(CrewCase):
    PANE = [f"pane {i}" for i in range(40)]
    DEAD = [f"dead {i}" for i in range(45)]

    def setUp(self):
        super().setUp()
        self.attached("--manager", "m1")
        self.path = roster.events(roster.directory("m1", driver=self.api), create=False)

    def run_event(self, event, *rows):
        self.api.rows.update((r.name, r) for r in rows)
        with open(self.path, "w") as f:
            f.write(event + "\n")
        return self.run_main("next-event", "--manager", "m1", "--after", "0", "--gen", "0")

    def live(self, reply=None):
        self.api.tuis["w1"] = (reply, "\n".join(self.PANE))
        return row("w1", SID)

    def dead(self, name="w1"):
        return row(name, SID, "dead", last_lines="\n".join(self.DEAD))

    def test_worker_done_prints_the_first_80_lines_of_its_reply(self):
        reply = [f"r{i}" for i in range(100)]
        got = self.run_event("10:00:01 w1 done", self.live("\n".join(reply)))
        self.assertEqual(got, (0, nl("1 10:00:01 w1 done", *reply[:80]), ""))
        self.assertEqual(self.api.ran("screen"), [])

    def test_done_without_a_reply_prints_the_pane(self):
        for why, made, lines in (
            ("no turn yet", lambda: self.live(None), self.PANE),
            ("not a tui session, dead", self.dead, self.DEAD[-40:]),
        ):
            with self.subTest(why):
                self.api.tuis.clear()
                got = self.run_event("10:00:01 w1 done", made())
                self.assertEqual(got, (0, nl("1 10:00:01 w1 done", *lines), ""))

    def test_blocked_and_dead_print_the_pane(self):
        for kind in ("blocked", "dead"):
            for state, made, lines in (
                ("live", self.live, self.PANE),
                ("dead", self.dead, self.DEAD[-40:]),
            ):
                with self.subTest(kind=kind, state=state):
                    self.api = Api(None)
                    got = self.run_event(f"10:00:01 w1 {kind}", made())
                    self.assertEqual(got, (0, nl(f"1 10:00:01 w1 {kind}", *lines), ""))
                    self.assertEqual(
                        self.api.ran("screen"), [("w1", 40)] if state == "live" else []
                    )
                    self.assertEqual(self.api.ran("attach"), ["w1"] if state == "live" else [])

    def test_a_dead_pane_prints_only_its_last_40_lines_blank_ones_kept(self):
        lines = ["error", *[""] * 40, "Pane is dead (status 2)"]
        got = self.run_event(
            "10:00:01 w1 dead", row("w1", SID, "dead", last_lines="\n".join(lines))
        )
        self.assertEqual(got, (0, nl("1 10:00:01 w1 dead", *lines[-40:]), ""))

    def test_other_events_print_nothing_more(self):
        for event in ("10:00:01 w1-drive outcome done", "10:00:01 w1 new kind"):
            with self.subTest(event=event):
                self.api = Api(None)
                self.assertEqual(self.run_event(event, self.live("r")), (0, nl(f"1 {event}"), ""))
                self.assertEqual(self.api.ran("sessions"), [None])
                self.assertEqual(self.api.ran("attach"), [])

    def test_empty_reply_prints_nothing_more(self):
        got = self.run_event("10:00:01 w1 done", self.live(""))
        self.assertEqual(got, (0, nl("1 10:00:01 w1 done"), ""))
        self.assertEqual(self.api.ran("screen"), [])

    def test_a_done_events_reply_is_read_only_after_configure(self):
        seen, attach = [], self.api.attach
        self.api.attach = lambda name: seen.append(self.api) or attach(name)
        got = self.run_event("10:00:01 w1 done", self.live("r"))
        self.assertEqual(got, (0, nl("1 10:00:01 w1 done", "r"), ""))
        self.assertEqual(seen, [self.api])

    def test_content_uses_the_supplied_driver_despite_an_unrelated_config(self):
        self.local("bogus = 1\n")
        rc, out, err = self.run_event("10:00:01 w1 done", self.live("r"))
        self.assertEqual((rc, out, err), (0, "1 10:00:01 w1 done\nr\n", ""))
        self.assertEqual(self.api.ran("attach"), ["w1"])

    def test_a_content_failure_is_printed_on_stdout_exit_0(self):
        cases = (
            ("10:00:01 w1 done", (), "workers: w1: no tmux session"),
            ("10:00:01 w1 blocked", (row("w1", SID),), "workers: no tui session w1"),
            ("10:00:01 a.b dead", (), "workers: a.b: no tmux session"),
        )
        for event, rows, msg in cases:
            with self.subTest(event=event, msg=msg):
                self.api = Api(None)
                self.assertEqual(self.run_event(event, *rows), (0, nl(f"1 {event}", msg), ""))


def numbered(count, start=1):
    return nl(*(f"12:00:00 w{i} done ".ljust(WIDTH - 1, ".") for i in range(start, start + count)))


def tail(directory, n=0, gen=0, *backlog):
    me, events = (
        f"env PYTHONPATH={shlex.quote(PYTHONPATH)} python3 {' '.join(MODULE)}",
        shlex.quote(os.path.join(directory, "events")),
    )
    name = os.path.basename(directory)
    return nl(
        *backlog,
        f"N={n} GEN={gen}",
        f"monitor: tail -n +{n + 1} -F {events}",
        f"monitor expired: {me} attach --manager {name} --after LINE --gen {gen} --resume",
        f"next-event: {me} next-event --manager {name} --after {n} --gen {gen}",
    )


def entry(kind="worker", **over):
    return {
        "kind": kind,
        "sid": SID,
        "cwd": "/w",
        "resume": [PY, SCRIPT[kind], "--go"],
        "note": None,
        "opener": None,
        "pane": None,
        "split": None,
        "split_from": None,
        "tui": None,
        "state": "working",
        "started": WHEN,
        **over,
    }


def result(**fields):
    return json.dumps({"ts": "2026-10-09T12:00:00", "kind": "result", **fields}) + "\n"


class RosterCase(CrewCase):
    def setUp(self):
        super().setUp()
        self.mdir = os.path.join(self.agent_pm, "managers", "mgr")
        self.path = os.path.join(self.mdir, "roster.json")
        self.lock = os.path.join(self.mdir, "roster.lock")
        self.api, self.runs = Api(), Runs()
        self.enterContext(
            mock.patch.object(subprocess, "run", lambda *a, **kw: self.runs(*a, **kw))
        )

    def seed(self, holder=None, cursor=0, gen=0, **entries):
        roster.ensure(self.mdir)
        with open(self.path, "w") as f:
            json.dump(
                {"version": 1, "holder": holder, "cursor": cursor, "gen": gen, "entries": entries},
                f,
            )
        return self.bytes()

    def bytes(self):
        with open(self.path, "rb") as f:
            return f.read()

    def entries(self):
        return json.loads(self.bytes())["entries"]

    def held(self):
        return (
            f"workers: manager directory {self.mdir}: held by tmux session other since {WHEN}; ask that manager to "
            "run crew.py release, or end that session\n"
        )

    def not_attached(self):
        return f"workers: manager directory {self.mdir} not attached: run crew.py attach first\n"


class AttachTest(RosterCase):
    def setUp(self):
        super().setUp()
        self.shows, self.then = [], None

    def server(self, own="mgr", rows=()):
        fake = Api(own, rows)
        fake.placed = ("%9", "mgr")

        def shown(session):
            self.shows.append((session, lock_free(self.lock)))
            if self.then:
                self.then()

        fake.on_show = shown
        return fake

    def attach(self, fake, *argv):
        return self.main_with(fake, "attach", *argv)

    def synced(self, fake, *argv):
        rc, out, err = self.attach(fake, *argv)
        shown = [f"tui: session {s}: tmux attach -t '={s}'\n" for s, _, _ in fake.shows]
        self.assertEqual((rc, err), (0, "".join(shown)))
        return out

    def test_first_attach_makes_the_directory_and_files_and_takes_the_lease(self):
        with fixed_time(WHEN):
            self.assertEqual(self.synced(self.server()), HEADER + tail(self.mdir))
        self.assertEqual(stat.S_IMODE(os.stat(self.mdir).st_mode), 0o700)
        for name in ("events", "roster.json", "roster.lock"):
            self.assertEqual(
                stat.S_IMODE(os.stat(os.path.join(self.mdir, name)).st_mode), 0o600, name
            )
        self.assertEqual(
            json.loads(self.bytes()),
            {"version": 1, "holder": MGR, "cursor": 0, "gen": 0, "entries": {}},
        )
        with fixed_time("2026-10-10T00:00:00+00:00"):
            self.synced(self.server())
        self.assertEqual(json.loads(self.bytes())["holder"], MGR)

    def test_manager_option_names_the_directory_the_own_session_holds(self):
        self.synced(self.server("m2"), "--manager", "mgr")
        self.assertEqual(json.loads(self.bytes())["holder"]["session"], "m2")

    def test_refused_by_a_live_other_holder_writing_nothing(self):
        for own in ("mgr", None):
            with self.subTest(own=own):
                before = self.seed(holder=OTHER, w1=entry())
                fake = self.server(own, [row("other")])
                self.assertEqual(self.attach(fake, "--manager", "mgr"), (1, "", self.held()))
                self.assertEqual(self.bytes(), before)
                self.assertEqual(fake.ran("sessions"), [None, "other"])

    def test_outside_tmux_prints_the_notice_and_leaves_the_holder(self):
        notice = f"workers: not in tmux: no lease on {self.mdir}; another manager may attach\n"
        for holder in (None, {"session": "old", "since": WHEN}):
            with self.subTest(holder=holder):
                self.seed(holder=holder)
                self.assertEqual(
                    self.attach(self.server(None), "--manager", "mgr"),
                    (0, HEADER + tail(self.mdir), notice),
                )
                self.assertEqual(json.loads(self.bytes())["holder"], holder)

    def test_outside_tmux_without_manager_exits_1_and_makes_nothing(self):
        self.assertEqual(
            self.attach(self.server(None)),
            (1, "", "workers: no manager directory: run inside tmux or give --manager <name>\n"),
        )
        self.assertEqual(self.made(), [])

    def test_a_worker_renamed_in_tmux_is_rekeyed_by_its_sid(self):
        old = entry(resume=[PY, SCRIPT["worker"], "start", "w1"], state="gone")
        self.seed(w1=old)
        self.synced(self.server(rows=[row("w9", SID, "done", "%4", "mgr", shown="mgr")]))
        self.assertEqual(
            self.entries(), {"w9": {**old, "state": "done", "pane": "%4", "opener": "mgr"}}
        )

    def test_no_rename_without_exactly_one_match_free_to_take(self):
        cases = {
            "two sessions have its sid": ([row("w8", SID), row("w9", SID)], {}),
            "the match is an entry": ([row("w9", SID)], {"w9": entry("role", sid=SID2)}),
            "no session has its sid": ([row("w9", SID2)], {}),
        }
        for why, (rows, others) in cases.items():
            with self.subTest(why):
                self.seed(w1=entry(), **others)
                rows = [dataclasses.replace(r, shown="mgr") for r in rows]
                self.synced(self.server(rows=rows))
                self.assertEqual(set(self.entries()), {"w1", *others})
                self.assertEqual(self.entries()["w1"]["state"], "gone")

    def test_only_a_worker_with_a_sid_is_renamed(self):
        self.seed(
            r1=entry("role", cwd=self.dir),
            p1=entry("pipeline", sid=SID2, cwd=self.dir),
            w1=entry(sid=None),
        )
        rows = [row("x1", SID, shown="mgr"), row("x2", SID2, shown="mgr"), row("x3", shown="mgr")]
        self.synced(self.server(rows=rows))
        self.assertEqual(
            {n: e["state"] for n, e in self.entries().items()},
            {"r1": "gone", "p1": "gone", "w1": "gone"},
        )

    def test_a_live_workers_state_is_its_rows(self):
        states = ("working", "done", "blocked", "dead")
        self.seed(**{f"w{i}": entry(state="gone") for i in range(len(states))})
        rows = [row(f"w{i}", SID, state, shown="mgr") for i, state in enumerate(states)]
        self.synced(self.server(rows=rows))
        self.assertEqual(
            [self.entries()[f"w{i}"]["state"] for i in range(len(states))], list(states)
        )

    def test_a_worker_without_a_session_is_gone(self):
        self.seed(w1=entry(state="blocked", pane="%4"))
        self.assertEqual(
            self.synced(self.server()),
            HEADER + f"w1\tworker\t{SID}\tgone\t-\t/w\t%4\n"
            f"resume w1: {recovered([PY, *MODULE, '--go'])}\n" + tail(self.mdir),
        )
        self.assertEqual(self.entries()["w1"]["state"], "gone")
        self.assertEqual(self.shows, [])

    def test_a_worker_without_a_session_is_gone_whatever_run_record_its_cwd_holds(self):
        entries = {}
        for status in ("needs_input", "done", "failed"):
            cwd = os.path.join(self.dir, status)
            os.mkdir(cwd)
            with open(os.path.join(cwd, "run.jsonl"), "w") as f:
                f.write(result(outcome={"status": status}))
            entries[status] = entry(cwd=cwd)
        self.seed(**entries)
        out = self.synced(self.server())
        self.assertEqual(
            {n: e["state"] for n, e in self.entries().items()}, dict.fromkeys(entries, "gone")
        )
        self.assertEqual(
            [line.split(":")[0] for line in out.splitlines() if line.startswith("resume ")],
            [f"resume {name}" for name in sorted(entries)],
        )

    def test_a_role_or_pipeline_is_live_by_its_driver_or_tui_session(self):
        for kind in ("role", "pipeline"):
            with self.subTest(kind=kind):
                self.shows = []
                self.seed(
                    d1=entry(kind, cwd=self.dir, state="gone", tui="t1", pane="%1"),
                    d2=entry(kind, cwd=self.dir, state="done"),
                    t3=entry(kind, cwd=self.dir, state="gone", tui="t3t"),
                    b4=entry(kind, cwd=self.dir, state="gone", tui="t4"),
                )
                rows = [
                    row("d1", state="dead", pane="%7", opener="x"),
                    row("d2", state="blocked"),
                    row("t3t", SID, "blocked", "%8", "mgr", shown="mgr"),
                    row("b4", state="dead"),
                    row("t4", state="done", pane="%6", shown="mgr"),
                ]
                self.synced(self.server(rows=rows))
                self.assertEqual(
                    {n: (e["state"], e["pane"], e["opener"]) for n, e in self.entries().items()},
                    {
                        "d1": ("working", "%1", None),
                        "d2": ("working", None, None),
                        "t3": ("blocked", "%8", "mgr"),
                        "b4": ("done", "%6", None),
                    },
                )
                self.assertEqual(self.shows, [])

    def test_a_role_or_pipeline_without_a_session_is_finished_waiting_or_gone_by_its_run_record(
        self,
    ):
        done, failed, run_tail = (
            result(outcome={"status": "done"}),
            result(outcome={"status": "failed"}),
            crew.RUN_TAIL,
        )
        asking = result(outcome={"status": "needs_input"})
        pad = ("x" * 99 + "\n") * (run_tail // 100 + 1)
        cut = run_tail - len(done)
        records = {
            "done": (done, "finished"),
            "failed": (failed, "finished"),
            "later-lines-skipped": (
                done + '{"kind": "end"}\nnot json\n' + "[" * 200000 + '\n{"kind": "res',
                "finished",
            ),
            "last-result-wins": (done + result(error="stopped: KeyboardInterrupt"), "gone"),
            "failed-after-an-error": (result(error="boom") + failed, "finished"),
            "needs-input": (asking, "waiting"),
            "answered": (asking + done, "finished"),
            "asking-after-done": (done + asking, "waiting"),
            "needs-input-after-an-error": (result(error="boom") + asking, "waiting"),
            "error-after-needs-input": (
                asking + result(error="stopped: KeyboardInterrupt"),
                "gone",
            ),
            "error": (result(error="boom"), "gone"),
            "malformed": ("{\nnot json\n\udcff\n", "gone"),
            "empty": ("", "gone"),
            "outcome-a-string": (result(outcome="done"), "gone"),
            "outcome-a-list": (result(outcome=[{"status": "done"}]), "gone"),
            "status-a-list": (result(outcome={"status": ["done"]}), "gone"),
            "status-an-object": (result(outcome={"status": {"needs_input": 1}}), "gone"),
            "status-other": (result(outcome={"status": "waiting"}), "gone"),
            "no-status": (result(outcome={}), "gone"),
            "kind-outcome": (
                json.dumps({"kind": "outcome", "outcome": {"status": "done"}}) + "\n",
                "gone",
            ),
            "a-list-line": (
                json.dumps([{"kind": "result", "outcome": {"status": "done"}}]) + "\n",
                "gone",
            ),
            "beyond-the-tail": (done + pad, "gone"),
            "within-the-tail": (pad + done, "finished"),
            "cut-by-the-tail": ("x" + done + "p\n" * (cut // 2) + "p" * (cut % 2), "gone"),
            "starts-the-tail": ("x\n" + done + "p\n" * (cut // 2) + "p" * (cut % 2), "finished"),
        }
        entries, want = {}, {}
        for i, (name, (text, state)) in enumerate(records.items()):
            cwd = os.path.join(self.dir, name)
            os.mkdir(cwd)
            with open(
                os.path.join(cwd, "run.jsonl"), "w", encoding="utf-8", errors="surrogateescape"
            ) as f:
                f.write(text)
            entries[name], want[name] = entry(("role", "pipeline")[i % 2], cwd=cwd), state
        done_cwd = os.path.join(self.dir, "done", "run.jsonl")
        for name, make in (
            ("missing", lambda path: None),
            ("symlink", lambda path: os.symlink(done_cwd, path)),
            ("fifo", os.mkfifo),
            ("directory", os.mkdir),
        ):
            cwd = os.path.join(self.dir, name)
            os.mkdir(cwd)
            make(os.path.join(cwd, "run.jsonl"))
            entries[name], want[name] = entry("role", cwd=cwd), "gone"
        self.seed(**entries)
        with within():
            self.synced(self.server())
        self.assertEqual({n: e["state"] for n, e in self.entries().items()}, want)

    def test_a_live_sessions_pane_and_opener_are_synced_when_valid(self):
        cases = {
            "w1": ("%7", "mgr", "%7", "mgr"),
            "w2": ("5E1B-C0FFEE", "w0t0p0:5E1B-C0FFEE", "5E1B-C0FFEE", "w0t0p0:5E1B-C0FFEE"),
            "w3": (None, None, "%1", "old"),
            "w4": (None, "new", "%1", "new"),
            "w5": ("%8", "a" * 201, "%8", "old"),
        }
        self.seed(**{name: entry(pane="%1", opener="old") for name in cases})
        rows = [row(n, SID, "working", p, o, shown="mgr") for n, (p, o, _, _) in cases.items()]
        self.synced(self.server(rows=rows))
        self.assertEqual(
            {n: (e["pane"], e["opener"]) for n, e in self.entries().items()},
            {n: (p, o) for n, (_, _, p, o) in cases.items()},
        )

    def test_a_live_session_no_client_shows_is_reopened_with_the_lock_free(self):
        self.seed(
            w1=entry(split="below", split_from="mgr"),
            r1=entry("role", tui="r1-tui", split="right"),
            w2=entry(),
            w3=entry(sid=SID2),
            r2=entry("role", sid=SID2, cwd=self.dir, tui="r2-tui"),
        )
        fake = self.server(
            rows=[
                row("w1", SID),
                row("r1-tui", state="done"),
                row("w2", SID, shown="mgr"),
                row("r2"),
            ]
        )
        with mock.patch("fcntl.flock", wraps=fcntl.flock) as locked:
            out = self.synced(fake)
        self.assertEqual(self.shows, [("r1-tui", True), ("w1", True)])
        self.assertEqual(fake.shows, [("r1-tui", "right", None), ("w1", "below", "mgr")])
        self.assertEqual(locked.call_count, 4)
        self.assertEqual(
            {n: (e["pane"], e["opener"]) for n, e in self.entries().items()},
            {
                "w1": ("%9", "mgr"),
                "r1": ("%9", "mgr"),
                "w2": (None, None),
                "w3": (None, None),
                "r2": (None, None),
            },
        )
        self.assertIn(f"w1\tworker\t{SID}\tworking\t-\t/w\t%9\n", out)

    def test_no_reopen_no_second_roster_block(self):
        self.seed(w1=entry())
        with mock.patch("fcntl.flock", wraps=fcntl.flock) as locked:
            self.synced(self.server(rows=[row("w1", SID, shown="mgr")]))
        self.assertEqual((locked.call_count, self.shows), (1, []))

    def test_placement_is_recorded_only_for_an_entry_still_the_same(self):
        def drop():
            with roster.locked(self.mdir) as r:
                roster.remove(r, "w1")

        cases = {
            "same": (None, {**entry(), "pane": "%9", "opener": "mgr"}),
            "sid changed": (lambda: roster.put(self.mdir, "w1", entry(sid=SID2)), entry(sid=SID2)),
            "kind changed": (
                lambda: roster.put(self.mdir, "w1", entry("pipeline")),
                entry("pipeline"),
            ),
            "removed": (drop, None),
        }
        for why, (then, want) in cases.items():
            with self.subTest(why):
                self.seed(w1=entry())
                self.then = then
                self.synced(self.server(rows=[row("w1", SID)]))
                self.assertEqual(self.entries().get("w1"), want)

    def test_a_shown_session_is_marked_by_the_session_showing_it(self):
        cases = {"w1": ("mgr", "mgr"), "w2": ("other", "other"), "w3": ("", "a terminal")}
        self.seed(**{name: entry() for name in cases})
        rows = [row(n, SID, shown=shown) for n, (shown, _) in cases.items()]
        self.assertEqual(
            self.synced(self.server(rows=rows)),
            HEADER
            + "".join(
                f"{n}\tworker\t{SID}\tworking\t-\t/w\t-\tshown in {where}\n"
                for n, (_, where) in cases.items()
            )
            + tail(self.mdir),
        )
        self.assertEqual(self.shows, [])

    def test_table_and_recovery_commands_none_for_a_waiting_run(self):
        cwd = os.path.join(self.dir, "it's a role")
        finished, asking = os.path.join(self.dir, "finished"), os.path.join(self.dir, "asking")
        for d, status in ((cwd, None), (finished, "done"), (asking, "needs_input")):
            os.mkdir(d)
            if status:
                with open(os.path.join(d, "run.jsonl"), "w") as f:
                    f.write(result(outcome={"status": status}))
        self.seed(
            b=entry(note="check the logs", pane="%4"),
            a=entry(sid=None, resume=[PY, "/x/crew.py", "; rm -rf ~"]),
            B=entry("role", cwd=cwd, resume=[PY, "/x/drive.py", "--role", "pm"]),
            C=entry("role", sid=None, cwd=cwd),
            p1=entry("pipeline", cwd=cwd, resume=[PY, "/x/router.py", "--plan", "/p q"]),
            f1=entry("pipeline", cwd=finished),
            n1=entry("role", cwd=asking),
            n2=entry("pipeline", cwd=asking),
        )
        out = self.synced(self.server(rows=[row("b", SID, "working", "%4", shown="mgr")]))
        role = (
            f"cd {shlex.quote(cwd)} && {PY} /x/drive.py --role pm --sid {SID} --resume "
            "--input 'Continue the unfinished task.'"
        )
        self.assertEqual(
            out,
            HEADER
            + nl(
                f"B\trole\t{SID}\tgone\t-\t{cwd}\t-",
                f"C\trole\t-\tgone\t-\t{cwd}\t-",
                "a\tworker\t-\tgone\t-\t/w\t-",
                f"b\tworker\t{SID}\tworking\tcheck the logs\t/w\t%4\tshown in mgr",
                f"f1\tpipeline\t{SID}\tfinished\t-\t{finished}\t-",
                f"n1\trole\t{SID}\twaiting\t-\t{asking}\t-",
                f"n2\tpipeline\t{SID}\twaiting\t-\t{asking}\t-",
                f"p1\tpipeline\t{SID}\tgone\t-\t{cwd}\t-",
                f"resume B: {role}",
                "resume C: none (no sid)",
                f"resume a: {recovered([PY, *MODULE, '; rm -rf ~'])}",
                f"resume p1: {PY} /x/router.py --plan '/p q'",
            )
            + tail(self.mdir),
        )
        self.assertEqual(shlex.split(role)[:4], ["cd", cwd, "&&", PY])
        self.assertEqual(
            shlex.split(out.splitlines()[-6].split(": ", 1)[1]),
            ["env", f"PYTHONPATH={PYTHONPATH}", PY, *MODULE, "; rm -rf ~"],
        )

    def test_a_worker_entrys_resume_line_names_the_import_root_then_the_callers_pythonpath(self):
        e = entry(resume=started("w1", "--client", "rt0"))
        self.seed(w1=e)
        with mock.patch.dict(os.environ, {"PYTHONPATH": f"{PYTHONPATH}:/u 1"}):
            out = self.synced(self.server())
        (line,) = (x for x in out.splitlines() if x.startswith("resume w1: "))
        self.assertEqual(
            shlex.split(line.removeprefix("resume w1: ")),
            ["env", f"PYTHONPATH={PYTHONPATH}:/u 1", *roster.recovery(e)],
        )

    def test_a_worker_entry_without_a_client_recovers_with_the_configs_runtime(self):
        resume = [PY, "/x/crew.py", "start", "w1", "--manager", "mgr"]
        self.seed(w1=entry(resume=resume))
        out = self.synced(self.server())
        self.assertIn(
            f"resume w1: {recovered(started('w1', '--client', 'rt0', '--manager', 'mgr'))}\n", out
        )
        self.local('runtime = ""\n')
        self.assertEqual(
            self.attach(self.server()),
            (1, "", "workers: config: runtime: want a runtime name\n"),
        )

    def test_an_invalid_entry_is_skipped_with_one_stderr_line(self):
        self.seed(
            w1=entry(), bad=entry(sid="; rm -rf ~"), rel=entry(resume=["python3", "/x/crew.py"])
        )
        rc, out, err = self.attach(self.server(rows=[row("w1", SID, shown="")]))
        self.assertEqual(
            (rc, out),
            (
                0,
                HEADER
                + f"w1\tworker\t{SID}\tworking\t-\t/w\t-\tshown in a terminal\n"
                + tail(self.mdir),
            ),
        )
        self.assertEqual(
            err,
            "manager: roster.json: entry 'bad': sid: '; rm -rf ~': want a session id or null\n"
            "manager: roster.json: entry 'rel': resume[0]: 'python3': want an absolute path\n",
        )
        self.assertEqual(set(self.entries()), {"w1"})

    def test_no_tmux_server_means_no_live_session(self):
        self.seed(w1=entry())
        rc, out, _ = self.attach(self.server(None), "--manager", "mgr")
        self.assertEqual((rc, out.splitlines()[1]), (0, f"w1\tworker\t{SID}\tgone\t-\t/w\t-"))
        self.assertEqual(self.entries()["w1"]["state"], "gone")

    def test_unknown_sessions_write_nothing(self):
        msg = "workers: tmux sessions unknown: tmux failed\n"
        for after in (0, 1):
            with self.subTest(after=after):
                before = self.seed(MGR, w1=entry())
                fake = self.server()
                fake.error, fake.error_after = DriverError(msg[9:-1]), after
                self.assertEqual(self.attach(fake, "--manager", "mgr"), (1, "", msg))
                self.assertEqual(self.bytes(), before)
        os.unlink(self.path)
        fake = self.server()
        fake.error, fake.error_after = DriverError("x"), 1
        self.assertEqual(self.attach(fake, "--manager", "mgr")[0], 1)
        self.assertFalse(os.path.exists(self.path))

    def test_a_reopen_prints_the_attach_line_on_stderr(self):
        self.seed(w1=entry())
        rc, out, err = self.attach(self.server(None, [row("w1", SID)]), "--manager", "mgr")
        self.assertEqual(
            (rc, out), (0, HEADER + f"w1\tworker\t{SID}\tworking\t-\t/w\t%9\n" + tail(self.mdir))
        )
        self.assertEqual(
            err,
            f"workers: not in tmux: no lease on {self.mdir}; another manager may attach\n"
            "tui: session w1: tmux attach -t '=w1'\n",
        )


class LeaseTest(RosterCase):
    CMDS = (["start", "w1"], ["next-event", "--after", "0", "--gen", "0"])
    FRESH = {"version": 1, "holder": None, "cursor": 0, "gen": 0, "entries": {}}

    def setUp(self):
        super().setUp()
        self.nxt = self.enterContext(
            mock.patch.object(
                self.event_source, "next", return_value=(1, "10:00:01 w1 outcome done")
            )
        )

    def rest(self, cmd):
        if not os.path.exists(self.path):
            return None
        r = json.loads(self.bytes())
        if cmd[0] == "start":
            del r["entries"]["w1"]
        return r

    def ran(self, fake, cmd):
        self.nxt.reset_mock()
        rc, _, err = self.main_with(fake, *cmd, "--manager", "mgr")
        err = "".join(x for x in err.splitlines(keepends=True) if not x.startswith("tui: "))
        return rc, err, bool(fake.starts) or self.nxt.called

    def test_refused_while_another_live_session_holds(self):
        before = self.seed(OTHER)
        for own in ("mgr", None):
            for cmd in self.CMDS:
                with self.subTest(own=own, cmd=cmd[0]):
                    self.assertEqual(
                        self.ran(Api(own, [row("other")]), cmd), (1, self.held(), False)
                    )
                    self.assertEqual(self.bytes(), before)

    def test_refused_in_tmux_when_not_the_holder(self):
        for holder in (None, OTHER):
            before = self.seed(holder)
            for cmd in self.CMDS:
                with self.subTest(holder=holder, cmd=cmd[0]):
                    self.assertEqual(self.ran(Api(), cmd), (1, self.not_attached(), False))
                    self.assertEqual(self.bytes(), before)

    def test_refused_when_the_directory_is_missing_which_stays_missing(self):
        for own in ("mgr", None):
            for cmd in self.CMDS:
                with self.subTest(own=own, cmd=cmd[0]):
                    self.assertEqual(self.ran(Api(own), cmd), (1, self.not_attached(), False))
                    self.assertEqual(self.made(), [])

    def test_allowed_for_the_holder(self):
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                before = json.loads(self.seed(MGR))
                self.assertEqual(self.ran(Api(), cmd), (0, "", True))
                self.assertEqual(self.rest(cmd), before)

    def test_allowed_outside_tmux_without_a_live_holder(self):
        roster.events(self.mdir)
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
                self.assertEqual(self.ran(Api(None), cmd), (0, "", True))
                self.assertEqual(self.rest(cmd), self.FRESH)
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0], holder="dead"):
                before = json.loads(self.seed(OTHER))
                self.assertEqual(self.ran(Api(None), cmd), (0, "", True))
                self.assertEqual(self.rest(cmd), before)


class StartEntryTest(RosterCase):
    def setUp(self):
        super().setUp()
        self.seed(MGR)

    def start(self, **kw):
        return start("w1", self.mdir, own="mgr", cwd=self.dir, driver=self.api, **kw)

    def resume(self, sid, *rest):
        return [
            sys.executable,
            *MODULE,
            "start",
            "w1",
            "--manager",
            "mgr",
            "--client",
            "rt0",
            "--resume",
            sid,
            "--cwd",
            self.dir,
            *rest,
        ]

    def lines(self, err):
        return [x for x in err.getvalue().splitlines() if not x.startswith("tui: ")]

    def test_the_worker_entry_resumes_through_the_crew_module(self):
        self.start()
        self.assertEqual(self.entries()["w1"]["resume"][:4], [sys.executable, *MODULE])
        self.assertEqual(roster.CREW, os.path.realpath(CREW))
        self.assertEqual(os.path.realpath(crew.__file__), roster.CREW)

    def test_the_worker_entry(self):
        cases = (
            ({}, (), []),
            ({}, ("--permission-mode", "auto"), ["--", "--permission-mode", "auto"]),
            ({}, ("--model=m", "--", "-c"), ["--", "--model=m", "--", "-c"]),
            ({"split": "below"}, (), ["--split", "below"]),
            ({"split_from": "x"}, ("--model", "m"), ["--split-from", "x", "--", "--model", "m"]),
            ({"split": "right", "split_from": "x"}, (), ["--split", "right", "--split-from", "x"]),
        )
        for kw, flags, rest in cases:
            with self.subTest(**kw, flags=flags):
                with fixed_time(WHEN):
                    sid = self.start(flags=flags, note="check it", **kw)
                self.assertEqual(
                    self.entries(),
                    {
                        "w1": entry(
                            sid=sid,
                            cwd=self.dir,
                            resume=self.resume(sid, *rest),
                            note="check it",
                            **kw,
                        )
                    },
                )

    def test_opener_and_pane_from_its_row_each_only_when_valid(self):
        cases = {
            "tmux": ("%5", "mgr", "%5", "mgr"),
            "iTerm2": ("5E1B-C0FFEE", "w0t0p0:5E1B-C0FFEE", "5E1B-C0FFEE", "w0t0p0:5E1B-C0FFEE"),
            "no pane": (None, "mgr", None, "mgr"),
            "no opener": ("%5", None, "%5", None),
            "an opener the roster refuses": ("%5", "a" * 201, "%5", None),
            "unset": (None, None, None, None),
        }
        for why, (pane, opener, *want) in cases.items():
            with self.subTest(why):
                self.api = Api()
                self.api.placed = (pane, opener)
                self.start()
                e = self.entries()["w1"]
                self.assertEqual([e["pane"], e["opener"]], want)

    def test_no_row_or_unknown_sessions_places_nothing(self):
        def unknown(name):
            self.api.error, self.api.error_after = DriverError("x"), 0

        for why, setup in (
            ("no row", lambda: setattr(self.api, "gone", True)),
            ("unknown", lambda: setattr(self.api, "on_show", unknown)),
        ):
            with self.subTest(why):
                self.api = Api()
                self.api.placed = ("%5", "mgr")
                setup()
                self.start()
                e = self.entries()["w1"]
                self.assertEqual((e["pane"], e["opener"]), (None, None))

    def test_a_worker_that_exits_at_once_leaves_a_working_entry_attach_syncs(self):
        self.api.events, self.api.dead = [Exited(1)], DEAD_TEXT
        with self.assertRaisesRegex(crew.WorkersError, "^w1: rt0 exited 1 at once"):
            self.start()
        sid = self.api.starts[0]["sid"]
        self.assertEqual(
            (self.entries()["w1"]["sid"], self.entries()["w1"]["state"]), (sid, "working")
        )
        rc, _, err = self.main_with(Api(rows=[row("w1", sid, "dead", shown="mgr")]), "attach")
        self.assertEqual((rc, err, self.entries()["w1"]["state"]), (0, "", "dead"))

    def test_a_re_start_replaces_the_entry_keeping_its_note_only_for_the_same_sid(self):
        first = self.start(note="keep")
        self.assertEqual(self.start(resume=first), first)
        self.assertEqual(self.entries()["w1"]["note"], "keep")
        self.start(resume=first, note="new")
        self.assertEqual(self.entries()["w1"]["note"], "new")
        second = self.start()
        self.assertNotEqual(second, first)
        self.assertEqual(
            {n: (e["sid"], e["note"]) for n, e in self.entries().items()}, {"w1": (second, None)}
        )

    def test_an_empty_note_is_none(self):
        self.start(note="")
        self.assertIsNone(self.entries()["w1"]["note"])

    def test_the_roster_lock_is_free_during_the_early_watch(self):
        free = []

        def watched():
            for _ in range(3):
                free.append(lock_free(self.lock))
                yield Activity()
            yield IdleTimeout(5)

        self.api.events = watched
        self.start()
        self.assertEqual(free, [True] * 3)

    def test_another_live_holder_starts_nothing(self):
        before = self.seed(OTHER)
        self.api.rows["other"] = row("other")
        with self.assertRaises(crew.WorkersError) as cm:
            self.start()
        self.assertEqual(f"workers: {cm.exception}\n", self.held())
        self.assertEqual((self.api.starts, self.bytes()), ([], before))

    def test_a_take_over_before_the_write_writes_nothing_the_worker_runs(self):
        def take_over(name):
            with roster.locked(self.mdir) as r:
                r["holder"] = OTHER
            self.api.rows["other"] = row("other")

        self.api.on_show = take_over
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.start(), self.api.starts[0]["sid"])
        held = self.held().removeprefix("workers: ").removesuffix("\n")
        self.assertEqual(self.lines(err), [f"manager: entry w1 not written: {held}"])
        self.assertEqual(self.entries(), {})
        self.assertEqual(self.api.ran("close"), [])

    def test_a_session_that_fails_to_start_writes_no_entry(self):
        def unwritable():
            os.mkdir(os.path.join(roster.worker_dir(SID), "worker.json"))

        for why, setup in (
            ("api.start", lambda: setattr(self.api, "start_error", DriverError("boom"))),
            ("the worker record", unwritable),
        ):
            with self.subTest(why):
                self.api = Api()
                setup()
                with self.assertRaises(crew.WorkersError):
                    self.start(resume=SID)
                self.assertEqual(self.entries(), {})

    def test_attach_prints_the_recovery_once_the_session_is_gone(self):
        sid = self.start(flags=("--permission-mode", "auto"))
        resume = self.resume(sid, "--", "--permission-mode", "auto")
        rc, out, err = self.main_with(Api(), "attach")
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.endswith(f"resume w1: {recovered(resume)}\n{tail(self.mdir)}"), out)


class RosterCommandsTest(RosterCase):
    CMDS = (["release"], ["note", "w1", "x"], ["forget", "w1"], ["stop", "w1"])

    def test_refused_without_the_lease_writing_nothing(self):
        cases = {
            "another live holder": (OTHER, [row("other")], self.held()),
            "no holder": (None, [], self.not_attached()),
            "a dead other holder": (OTHER, [], self.not_attached()),
        }
        for why, (holder, rows, msg) in cases.items():
            for cmd in self.CMDS:
                with self.subTest(why, cmd=cmd[0]):
                    before = self.seed(holder, w1=entry())
                    fake = Api(rows=[*rows, row("w1", SID)])
                    self.assertEqual(self.main_with(fake, *cmd), (1, "", msg))
                    self.assertEqual(fake.ran("close"), [])
                    self.assertEqual(self.bytes(), before)

    def test_refused_when_the_directory_is_missing_which_stays_missing(self):
        for own in ("mgr", None):
            for cmd in self.CMDS:
                with self.subTest(own=own, cmd=cmd[0]):
                    self.assertEqual(
                        self.main_with(Api(own), *cmd, "--manager", "mgr"),
                        (1, "", self.not_attached()),
                    )
                    self.assertEqual(self.made(), [])

    def test_session_free_commands_never_configure_and_work_with_a_bad_session_config(self):
        self.local('[hosts]\ndefault = "nope"\n')
        self.seed(MGR, w1=entry(), w2=entry(sid=SID2))
        for cmd in (["note", "w1", "x"], ["forget", "w2"], ["stop", "w1"], ["release"]):
            with self.subTest(cmd=cmd[0]):
                fake = Api(rows=[row("w1", SID)])
                self.assertEqual(self.main_with(fake, *cmd)[0], 0)

    def test_release_by_the_holder_keeps_the_cursor(self):
        self.seed(MGR, cursor=7, w1=entry())
        self.assertEqual(
            self.main_with(Api(), "release"), (0, f"workers: released {self.mdir}\n", "")
        )
        self.assertEqual(
            json.loads(self.bytes()),
            {"version": 1, "holder": None, "cursor": 7, "gen": 0, "entries": {"w1": entry()}},
        )

    def test_release_outside_tmux_without_a_live_holder(self):
        self.seed(OTHER, cursor=7)
        self.assertEqual(
            self.main_with(Api(None), "release", "--manager", "mgr"),
            (0, f"workers: released {self.mdir}\n", ""),
        )
        r = json.loads(self.bytes())
        self.assertEqual((r["holder"], r["cursor"]), (None, 7))

    def test_note_sets_and_an_empty_one_clears(self):
        self.seed(MGR, w1=entry(), w2=entry(note="old"), w3=entry())
        for argv in (["w1", "check the logs"], ["w2", ""], ["w3", "x" * 500]):
            self.assertEqual(self.main_with(Api(), "note", *argv), (0, "", ""))
        self.assertEqual(
            self.entries(),
            {"w1": entry(note="check the logs"), "w2": entry(), "w3": entry(note="x" * 500)},
        )

    def test_a_long_or_unprintable_note_is_refused_writing_nothing(self):
        cases = {
            "x" * 501: "note: 501 characters: want at most 500",
            **{
                text: f"note: {text!r}: {UNPRINTABLE}"
                for text in ("a\nb", "a\x9fb", "a b", "\x7f", "a\tb")
            },
        }
        for text, msg in cases.items():
            with self.subTest(msg):
                before = self.seed(MGR, w1=entry())
                self.assertEqual(
                    self.main_with(Api(), "note", "w1", text), (1, "", f"workers: {msg}\n")
                )
                self.assertEqual(self.bytes(), before)

    def test_note_forget_and_stop_need_an_entry_killing_nothing(self):
        for cmd in (["note", "w9", "x"], ["forget", "w9"], ["stop", "w9"]):
            with self.subTest(cmd=cmd[0]):
                before = self.seed(MGR, w1=entry())
                fake = Api(rows=[row("w9"), row("w1", SID)])
                self.assertEqual(self.main_with(fake, *cmd), (1, "", "workers: w9 not in roster\n"))
                self.assertEqual((self.bytes(), fake.ran("close")), (before, []))

    def test_note_forget_and_stop_check_the_name(self):
        for cmd in (["note", "a b", "x"], ["forget", "a b"], ["stop", "a b"]):
            with self.subTest(cmd=cmd[0]):
                before = self.seed(MGR, w1=entry())
                self.assertEqual(
                    self.main_with(Api(), *cmd),
                    (1, "", "workers: bad name 'a b': use [A-Za-z0-9_-]+\n"),
                )
                self.assertEqual(self.bytes(), before)

    def test_forget_removes_an_entry_no_session_keeps(self):
        self.seed(MGR, w1=entry(state="gone"), r1=entry("role", tui="r1-tui"), w2=entry())
        for name in ("w1", "r1"):
            self.assertEqual(self.main_with(Api(rows=[row("w2")]), "forget", name), (0, "", ""))
        self.assertEqual(self.entries(), {"w2": entry()})

    def test_forget_refuses_a_live_entry_writing_nothing(self):
        entries = {
            "w1": entry(),
            "r1": entry("role", tui="r1-tui"),
            "r2": entry("role", tui="r2-tui"),
            "p1": entry("pipeline", tui="p1-tui"),
        }
        fake = Api(rows=[row("w1"), row("r1"), row("r2-tui"), row("p1-tui")])
        for name, session in (("w1", "w1"), ("r1", "r1"), ("r2", "r2-tui"), ("p1", "p1-tui")):
            with self.subTest(name):
                before = self.seed(MGR, **entries)
                self.assertEqual(
                    self.main_with(fake, "forget", name),
                    (1, "", f"workers: {name}: session {session} is live\n"),
                )
                self.assertEqual(self.bytes(), before)

    def test_forget_after_unknown_sessions_writes_nothing(self):
        before = self.seed(MGR, w1=entry(state="gone"))
        fake = Api()
        fake.error, fake.error_after = DriverError("tmux sessions unknown: tmux failed"), 1
        self.assertEqual(
            self.main_with(fake, "forget", "w1"),
            (1, "", "workers: tmux sessions unknown: tmux failed\n"),
        )
        self.assertEqual(self.bytes(), before)


class StopTest(RosterCase):
    def stopped(self, fake, name, *argv):
        res = self.main_with(fake, "stop", name, *argv)
        return res, fake.ran("close"), set(self.entries())

    def test_a_worker(self):
        self.seed(MGR, w1=entry(), w2=entry(sid=SID2))
        fake = Api(rows=[row("w1", SID), row("w2", SID2)])
        self.assertEqual(self.stopped(fake, "w1"), ((0, "", ""), ["w1"], {"w2"}))
        self.assertEqual(set(fake.rows), {"mgr", "w2"})

    def test_a_role_or_pipeline_by_its_key_or_tui_its_driver_then_tui(self):
        for kind in ("role", "pipeline"):
            for name in ("d1", "t1"):
                with self.subTest(kind=kind, name=name):
                    self.seed(MGR, d1=entry(kind, tui="t1"), w1=entry())
                    fake = Api(rows=[row("d1"), row("t1"), row("w1", SID)])
                    self.assertEqual(self.stopped(fake, name), ((0, "", ""), ["d1", "t1"], {"w1"}))
                    self.assertEqual(set(fake.rows), {"mgr", "w1"})

    def test_sessions_gone_already(self):
        self.seed(
            MGR,
            w1=entry(state="gone"),
            d1=entry("role", tui="t1", state="finished"),
            d2=entry("role"),
            w2=entry(),
        )
        for name, closed, left in (
            ("w1", ["w1"], {"d1", "d2", "w2"}),
            ("t1", ["d1", "t1"], {"d2", "w2"}),
            ("d2", ["d2"], {"w2"}),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.stopped(Api(), name), ((0, "", ""), closed, left))

    def test_a_session_outliving_its_close_exits_1_keeping_the_entry(self):
        before = self.seed(MGR, w1=entry())
        fake = Api(rows=[row("w1", SID)])
        fake.stuck.add("w1")
        self.assertEqual(
            self.main_with(fake, "stop", "w1"),
            (1, "", "workers: w1: session w1 still live after tmux kill-session\n"),
        )
        self.assertEqual(self.bytes(), before)

    def test_outside_tmux_without_a_live_holder(self):
        for holder in (None, OTHER):
            with self.subTest(holder=holder):
                self.seed(holder, w1=entry(), w2=entry(sid=SID2))
                fake = Api(None, [row("w1", SID)])
                self.assertEqual(
                    self.stopped(fake, "w1", "--manager", "mgr"), ((0, "", ""), ["w1"], {"w2"})
                )
                self.assertEqual(json.loads(self.bytes())["holder"], holder)

    def test_a_stopped_entry_is_not_resumed(self):
        self.seed(MGR, w1=entry(resume=started("w1")), w2=entry(sid=SID2, resume=started("w2")))
        self.assertEqual(self.main_with(Api(rows=[row("w1", SID)]), "stop", "w1"), (0, "", ""))
        self.assertEqual(self.main_with(Api(), "attach", "--resume")[0], 0)
        self.assertEqual([Runs.key(argv) for argv, _, _ in self.runs.recoveries], ["w2"])


class StopAllTest(RosterCase):
    def test_selects_by_opener_recursively_deepest_first(self):
        openers = {
            "mgr": None,
            "w1": "mgr",
            "w2": "mgr",
            "w3": "w1",
            "w4": "w3",
            "x": "other",
            "y": "x",
            "z": None,
            "loop": "loop",
            "back": "w4",
        }

        def live(**over):
            return {n: row(n, opener=o) for n, o in {**openers, **over}.items()}

        self.assertEqual(crew.opened("mgr", live()), ["back", "w4", "w3", "w1", "w2"])
        self.assertEqual(crew.opened("mgr", live(mgr="w1")), ["back", "w4", "w3", "w1", "w2"])
        self.assertEqual(crew.opened("nobody", live()), [])
        entries = {"mgr": {"x": entry(), "w9": entry(tui="t9"), "w1": entry()}}
        self.assertEqual(
            crew.opened("mgr", live(w9=None, t9="mgr"), lambda s: entries.get(s, {})),
            ["back", "w4", "w3", "t9", "w1", "w2", "w9"],
        )

    def rows(self):
        return [
            row("w1", SID, pane="%1", opener="mgr"),
            row("w3", pane="%2", opener="w1"),
            row("d1", opener="mgr"),
            row("t1", pane="%3", opener="mgr"),
            row("agent-pm-engineer-TASK-1", opener="mgr"),
            row("o1", pane="%4", opener="other"),
        ]

    def test_dry_run_lists_all_and_stops_nothing(self):
        before = self.seed(
            MGR,
            w1=entry(),
            d1=entry("role", tui="t1"),
            **{"agent-pm-engineer-TASK-1": entry("pipeline")},
        )
        fake = Api(rows=self.rows())
        self.assertEqual(
            self.main_with(fake, "stop-all", "--dry-run"),
            (
                0,
                nl(
                    "would stop w3",
                    "would stop agent-pm-engineer-TASK-1; a scheduler may resume it",
                    "would stop d1",
                    "would stop t1",
                    "would stop w1",
                ),
                "",
            ),
        )
        self.assertEqual((fake.ran("close"), self.bytes()), ([], before))

    def test_stops_them_and_removes_their_entries(self):
        self.seed(
            MGR,
            w1=entry(),
            d1=entry("role", tui="t1"),
            gone=entry(state="gone"),
            **{"agent-pm-engineer-TASK-1": entry("pipeline")},
        )
        w1 = os.path.join(self.agent_pm, "managers", "w1")
        roster.ensure(w1)
        roster.put(w1, "w3", entry())
        fake = Api(rows=self.rows())
        self.assertEqual(
            self.main_with(fake, "stop-all"),
            (
                0,
                nl(
                    "stopped w3",
                    "stopped agent-pm-engineer-TASK-1; a scheduler may resume it",
                    "stopped d1",
                    "stopped t1",
                    "stopped w1",
                ),
                "",
            ),
        )
        self.assertEqual(fake.ran("close"), ["w3", "agent-pm-engineer-TASK-1", "d1", "t1", "w1"])
        self.assertEqual((set(fake.rows), set(self.entries())), ({"mgr", "o1"}, {"gone"}))
        with roster.locked(w1, write=False) as r:
            self.assertEqual(r["entries"], {})

    def test_unshown_sessions_of_its_roster_too_an_entry_kept_while_its_key_lives(self):
        self.seed(MGR, w1=entry(), d1=entry("role", tui="t1"))
        fake = Api(rows=[row("w1", SID), row("d1"), row("t1")])
        fake.stuck.add("d1")
        live = "workers: stop-all: still live after tmux kill-session: d1\n"
        self.assertEqual(self.main_with(fake, "stop-all"), (1, "stopped t1\nstopped w1\n", live))
        self.assertEqual(set(self.entries()), {"d1"})

    def test_nothing_to_stop(self):
        self.assertEqual(
            self.main_with(Api(rows=[row("o1", opener="other")]), "stop-all"),
            (0, "workers: stop-all: nothing to stop\n", ""),
        )

    def test_a_session_outliving_its_close_exits_1_keeping_its_entry(self):
        self.seed(MGR, w1=entry(), w2=entry())
        fake = Api(rows=[row("w1", opener="mgr"), row("w2", opener="mgr")])
        fake.stuck.add("w1")
        self.assertEqual(
            self.main_with(fake, "stop-all"),
            (1, "stopped w2\n", "workers: stop-all: still live after tmux kill-session: w1\n"),
        )
        self.assertEqual(set(self.entries()), {"w1"})

    def test_outside_tmux_or_not_the_holder_stops_nothing(self):
        self.assertEqual(
            self.main_with(Api(None, [row("w1", opener="mgr")]), "stop-all"),
            (1, "", "workers: stop-all: not in tmux: run it in the manager's tmux session\n"),
        )
        self.seed(OTHER, w1=entry())
        fake = Api(rows=[row("w1", opener="mgr"), row("other")])
        self.assertEqual(self.main_with(fake, "stop-all"), (1, "", self.held()))
        self.assertEqual(fake.ran("close"), [])


class ResumeTest(RosterCase):
    RUN = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "errors": "replace",
    }

    def test_each_gone_entry_runs_its_recovery_argv_in_its_cwd_unlocked_no_shell_in_table_order(
        self,
    ):
        self.enterContext(mock.patch.dict(os.environ, {"PYTHONPATH": f"{PYTHONPATH}:/u1"}))
        cwd, finished = os.path.join(self.dir, "it's a role"), os.path.join(self.dir, "finished")
        os.mkdir(finished)
        with open(os.path.join(finished, "run.jsonl"), "w") as f:
            f.write(result(outcome={"status": "done"}))
        entries = {
            "b": entry(cwd="/w/b", resume=started("b", "--client", "rt1")),
            "a": entry(cwd=cwd, resume=started("a", "; rm -rf ~")),
            "B": entry("role", cwd=cwd, resume=[PY, "/x/drive.py", "--role", "pm"]),
            "p1": entry("pipeline", cwd=cwd, resume=[PY, "/x/router.py", "--issue", "TASK-1"]),
            "f1": entry("pipeline", cwd=finished),
            "live": entry(sid=SID2),
        }
        self.seed(MGR, **entries)
        live = [row("live", SID2, shown="mgr")]
        rc, listed, err = self.main_with(Api(rows=live), "attach")
        self.assertEqual((rc, err, self.runs.recoveries), (0, "", []))
        self.runs = Runs(
            {
                "b": (0, f"tui: session b: tmux attach -t '=b'\nb {SID}\n"),
                "a": (0, "  a \n\n  \n"),
                "/x/drive.py": (0, "drive.py: session x\n"),
                "/x/router.py": (0, ""),
            },
            self.lock,
        )
        self.assertEqual(
            self.main_with(Api(rows=live), "attach", "--resume"),
            (
                0,
                listed
                + nl(
                    "resumed B: exit 0: drive.py: session x",
                    "resumed a: exit 0: a",
                    f"resumed b: exit 0: b {SID}",
                    "resumed p1: exit 0",
                ),
                "",
            ),
        )
        ran = {name: roster.recovery(entries[name]) for name in ("B", "a", "b", "p1")}
        env = {n: {"PYTHONPATH": f"{PYTHONPATH}:/u1"} if n in "ab" else {} for n in ran}
        self.assertEqual(
            self.runs.recoveries,
            [
                (
                    argv,
                    {"cwd": entries[n]["cwd"], "env": {**os.environ, **env[n]}, **self.RUN},
                    True,
                )
                for n, argv in ran.items()
            ],
        )
        printed = dict(
            line.removeprefix("resume ").split(": ", 1)
            for line in listed.splitlines()
            if line.startswith("resume ")
        )
        self.assertEqual(
            {n: shlex.split(cmd) for n, cmd in printed.items()},
            {
                **{
                    n: [*(["env", f"PYTHONPATH={env[n]['PYTHONPATH']}"] if env[n] else []), *argv]
                    for n, argv in ran.items()
                },
                "B": ["cd", cwd, "&&", *ran["B"]],
            },
        )

    def test_a_crew_recovery_gets_its_import_root_the_rest_the_users_pythonpath(self):
        entries = {
            "B": entry("role", cwd=self.dir),
            "b": entry(cwd=self.dir, resume=started("b")),
            "p1": entry("pipeline", cwd=self.dir),
        }
        for given, user in ((f"{PYTHONPATH}:/u1", "/u1"), (PYTHONPATH, None), (None, None)):
            with self.subTest(given=given), mock.patch.dict(os.environ):
                os.environ.pop("PYTHONPATH", None)
                if given is not None:
                    os.environ["PYTHONPATH"] = given
                self.seed(MGR, **entries)
                self.runs = Runs()
                self.assertEqual(self.main_with(Api(), "attach", "--resume")[0], 0)
                envs = {Runs.key(argv): kw["env"] for argv, kw, _ in self.runs.recoveries}
                self.assertEqual(
                    {k: env.get("PYTHONPATH") for k, env in envs.items()},
                    {
                        "/x/drive.py": user,
                        "b": PYTHONPATH if user is None else f"{PYTHONPATH}:{user}",
                        "/x/router.py": user,
                    },
                )
                with mock.patch.dict(os.environ, envs["b"], clear=True):
                    crew._drop_import_root()
                    self.assertEqual(os.environ.get("PYTHONPATH"), user)

    def test_a_failing_recovery_stops_none_and_attach_exits_1(self):
        refusal = "router.py: TASK-1 has a live agent run: tmux attach -t '=agent-pm-pm-TASK-1'"
        self.seed(
            MGR,
            a1=entry(resume=started("a1")),
            a2=entry(resume=started("a2")),
            a3=entry("role", sid=None),
            a4=entry(resume=started("a4")),
            p1=entry("pipeline", resume=[PY, "/x/router.py"]),
            w1=entry(resume=started("w1")),
        )
        self.runs = Runs(
            {
                "a1": (1, "workers: a1: session ended at once\n"),
                "a2": FileNotFoundError(errno.ENOENT, "No such file or directory"),
                "a4": ValueError("embedded null byte"),
                "/x/router.py": (1, refusal + "\n"),
            }
        )
        rc, out, err = self.main_with(Api(), "attach", "--resume")
        self.assertEqual((rc, err), (1, ""))
        self.assertTrue(
            out.endswith(
                tail(self.mdir)
                + nl(
                    "resumed a1: exit 1: workers: a1: session ended at once",
                    "resumed a2: not run: No such file or directory",
                    "resumed a3: not run: no sid",
                    "resumed a4: not run: ValueError: embedded null byte",
                    f"resumed p1: exit 1: {refusal}",
                    "resumed w1: exit 0",
                )
            ),
            out,
        )
        self.assertEqual(
            [Runs.key(argv) for argv, _, _ in self.runs.recoveries],
            ["a1", "a2", "a4", "/x/router.py", "w1"],
        )

    def test_one_failed_recovery_of_any_kind_exits_1(self):
        cases = {
            "exit 1": ((1, "boom\n"), "exit 1: boom"),
            "exit 255": ((255, ""), "exit 255"),
            "an OSError": (PermissionError(errno.EACCES, "denied"), "not run: denied"),
            "a ValueError": (
                ValueError("embedded null byte"),
                "not run: ValueError: embedded null byte",
            ),
            "a TypeError": (TypeError("bad argv"), "not run: TypeError: bad argv"),
            "no sid": (None, "not run: no sid"),
        }
        for why, (failure, line) in cases.items():
            with self.subTest(why):
                bad = entry("role", sid=None) if failure is None else entry(resume=started("bad"))
                self.seed(MGR, bad=bad, ok=entry(resume=started("ok")))
                self.runs = Runs({} if failure is None else {"bad": failure})
                rc, out, err = self.main_with(Api(), "attach", "--resume")
                self.assertEqual((rc, err), (1, ""))
                self.assertTrue(
                    out.endswith(
                        tail(self.mdir) + nl(f"resumed bad: {line}", "resumed ok: exit 0")
                    ),
                    out,
                )

    def test_a_waiting_run_is_left_alone(self):
        asking = os.path.join(self.dir, "asking")
        os.mkdir(asking)
        with open(os.path.join(asking, "run.jsonl"), "w") as f:
            f.write(result(outcome={"status": "needs_input", "questions": ["Which?"]}))
        self.seed(
            MGR,
            r1=entry("role", cwd=asking),
            p1=entry("pipeline", cwd=asking),
            w1=entry(cwd=asking, resume=started("w1")),
        )
        rc, out, err = self.main_with(Api(), "attach", "--resume")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            out,
            HEADER
            + nl(
                f"p1\tpipeline\t{SID}\twaiting\t-\t{asking}\t-",
                f"r1\trole\t{SID}\twaiting\t-\t{asking}\t-",
                f"w1\tworker\t{SID}\tgone\t-\t{asking}\t-",
                f"resume w1: {recovered(started('w1', '--client', 'rt0'))}",
            )
            + tail(self.mdir)
            + "resumed w1: exit 0\n",
        )
        self.assertEqual(
            [argv for argv, _, _ in self.runs.recoveries], [started("w1", "--client", "rt0")]
        )
        self.assertEqual(
            {n: e["state"] for n, e in self.entries().items()},
            {"p1": "waiting", "r1": "waiting", "w1": "gone"},
        )


class CursorTest(RosterCase):
    BIG = SPAN * 11 // 10
    ROTATE = "workers: rotate: run crew.py attach, then arm with its N and GEN\n"

    def setUp(self):
        super().setUp()
        self.mevents = os.path.join(self.mdir, "events")
        self.old = self.mevents + ".1"
        self.waits = []
        self.enterContext(mock.patch.object(self.event_source, "next", side_effect=self.wait))

    def wait(self, events, after):
        self.waits.append((events, after, json.loads(self.bytes())["cursor"]))
        return after + 1, "12:00:00 w1 outcome done"

    def fill(self, data, cursor=0, gen=0):
        before = self.seed(MGR, cursor=cursor, gen=gen)
        with open(self.mevents, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        return before

    def read(self, path=None):
        with open(path or self.mevents, "rb") as f:
            return f.read()

    def cursor(self):
        r = json.loads(self.bytes())
        return r["cursor"], r["gen"]

    def attach(self, *argv, own="mgr"):
        return self.main_with(Api(own), "attach", "--manager", "mgr", *argv)

    def next_event(self, after, gen=0):
        return self.main_with(Api(), "next-event", "--after", str(after), "--gen", str(gen))

    def test_attach_prints_the_backlog_after_the_cursor_then_n_gen_and_the_arm_commands(self):
        events = [f"12:00:0{i} w1 done" for i in range(1, 6)]
        self.fill(nl(*events), cursor=3)
        me = f"env PYTHONPATH={shlex.quote(PYTHONPATH)} python3 -P -m agent_pm.manager.crew"
        self.assertEqual(
            self.attach(),
            (
                0,
                HEADER
                + nl(
                    f"4 {events[3]}",
                    f"5 {events[4]}",
                    "N=3 GEN=0",
                    f"monitor: tail -n +4 -F {self.mevents}",
                    f"monitor expired: {me} attach --manager mgr --after LINE --gen 0 --resume",
                    f"next-event: {me} next-event --manager mgr --after 3 --gen 0",
                ),
                "",
            ),
        )
        self.assertEqual(self.cursor(), (3, 0))

    def test_the_backlog_skips_a_non_event_line_but_counts_it(self):
        self.fill(
            b"12:00:01 w1 done\ngarbage\n\n12:00:04 w1 caf\xc3\xa9 \xff\n12:00:05 w2 blocked\n12:00:06 w1 do",
            cursor=1,
        )
        self.assertEqual(
            self.attach(),
            (
                0,
                HEADER + tail(self.mdir, 1, 0, "4 12:00:04 w1 café �", "5 12:00:05 w2 blocked"),
                "",
            ),
        )

    def test_attach_names_the_crew_module_on_its_import_root_then_the_callers_pythonpath(self):
        for given, want in ((PYTHONPATH, PYTHONPATH), (f"{PYTHONPATH}:/u 1", f"{PYTHONPATH}:/u 1")):
            with self.subTest(given=given), mock.patch.dict(os.environ, {"PYTHONPATH": given}):
                rc, out, err = self.attach()
                self.assertEqual((rc, err), (0, ""))
                lines = out.splitlines()[-2:]
                self.assertEqual(
                    [line.split(": ", 1)[0] for line in lines], ["monitor expired", "next-event"]
                )
                self.assertEqual(
                    [shlex.split(line.split(": ", 1)[1])[:6] for line in lines],
                    [["env", f"PYTHONPATH={want}", "python3", *MODULE]] * 2,
                )

    def test_attach_quotes_the_paths_it_prints(self):
        home = os.path.join(self.dir, "it's a home")
        os.mkdir(home)
        root = os.path.join(self.dir, "my src")
        with (
            mock.patch.dict(os.environ, {"HOME": home, "PYTHONPATH": root}),
        ):
            rc, out, err = self.attach()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(
            [shlex.split(line.split(": ", 1)[1]) for line in out.splitlines()[-3:]],
            [
                [
                    "tail",
                    "-n",
                    "+1",
                    "-F",
                    os.path.join(home, ".agent-pm", "managers", "mgr", "events"),
                ],
                [
                    "env",
                    f"PYTHONPATH={PYTHONPATH}:{root}",
                    "python3",
                    *MODULE,
                    "attach",
                    "--manager",
                    "mgr",
                    "--after",
                    "LINE",
                    "--gen",
                    "0",
                    "--resume",
                ],
                [
                    "env",
                    f"PYTHONPATH={PYTHONPATH}:{root}",
                    "python3",
                    *MODULE,
                    "next-event",
                    "--manager",
                    "mgr",
                    "--after",
                    "0",
                    "--gen",
                    "0",
                ],
            ],
        )

    def test_attach_after_records_the_cursor_which_only_moves_forward(self):
        lines = numbered(9).splitlines()
        self.fill(numbered(9))
        self.assertEqual(
            self.attach("--after", "7", "--gen", "0"),
            (0, HEADER + tail(self.mdir, 7, 0, f"8 {lines[7]}", f"9 {lines[8]}"), ""),
        )
        self.assertEqual(self.next_event(5), (0, "6 12:00:00 w1 outcome done\n", ""))
        self.assertEqual((self.cursor(), self.waits), ((7, 0), [(self.mevents, 5, 7)]))
        self.assertEqual(self.attach()[1].splitlines()[-4], "N=7 GEN=0")
        self.assertEqual(self.cursor(), (7, 0))

    def test_attach_refuses_stale_line_numbers_writing_nothing(self):
        cases = {
            "an old gen": (
                ["--after", "7", "--gen", "0"],
                "stale line numbers (gen 0, now 1): run crew.py attach",
            ),
            "past the end": (
                ["--after", "10", "--gen", "1"],
                f"line 10 is past the end of {self.mevents} (9 lines): run crew.py attach",
            ),
        }
        for why, (argv, msg) in cases.items():
            with self.subTest(why):
                before = self.fill(numbered(9), cursor=2, gen=1)
                self.assertEqual(self.attach(*argv), (1, "", f"workers: {msg}\n"))
                self.assertEqual(self.bytes(), before)

    def test_attach_after_and_gen_go_together_as_numbers(self):
        alone = "crew.py: error: --after and --gen go together\n"
        for argv, want in (
            (["--after", "3"], alone),
            (["--gen", "0"], alone),
            (["--after", "end", "--gen", "0"], None),
            (["--after", "-1", "--gen", "0"], None),
            (["--after", "1", "--gen", "x"], None),
        ):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    crew.main(["attach", "--manager", "mgr", *argv], driver=self.api)
                self.assertEqual(cm.exception.code, 2)
                if want:
                    self.assertTrue(err.getvalue().endswith(want), err.getvalue())
                self.assertEqual(self.made(), [])

    def test_attach_rotates_keeping_the_lines_after_the_cursor(self):
        data = numbered(self.BIG)
        backlog = [f"{i} {line}" for i, line in enumerate(data.splitlines()[-3:], 1)]
        cases = {
            "at the cursor": (self.BIG - 3, []),
            "--after below the cursor": (self.BIG - 3, ["--after", "5", "--gen", "0"]),
            "--after recorded first": (0, ["--after", str(self.BIG - 3), "--gen", "0"]),
        }
        for why, (cursor, argv) in cases.items():
            with self.subTest(why):
                self.sleep.reset_mock()
                self.fill(data, cursor)
                ino = os.lstat(self.mevents).st_ino
                self.assertEqual(
                    self.attach(*argv), (0, HEADER + tail(self.mdir, 0, 1, *backlog), "")
                )
                self.assertEqual(self.cursor(), (0, 1))
                self.assertEqual(
                    (os.lstat(self.old).st_ino, self.read(self.old)), (ino, data.encode())
                )
                self.assertEqual(self.read(), "".join(data.splitlines(keepends=True)[-3:]).encode())
                self.sleep.assert_called_once_with(roster.SETTLE)
        before = self.bytes()
        self.assertEqual(
            self.attach("--after", "2", "--gen", "0"),
            (1, "", "workers: stale line numbers (gen 0, now 1): run crew.py attach\n"),
        )
        self.assertEqual(self.bytes(), before)

    def test_attach_refuses_a_symlinked_events_1_renaming_nothing(self):
        data = numbered(self.BIG).encode()
        before = self.fill(data, self.BIG - 3)
        os.symlink(os.path.join(self.dir, "target"), self.old)
        self.assertEqual(
            self.attach(), (1, "", f"workers: {self.old}: not a regular file owned by you\n")
        )
        self.assertEqual(
            (self.bytes(), self.read(), os.path.islink(self.old)), (before, data, True)
        )

    def test_unknown_sessions_with_a_rotation_due_renames_nothing(self):
        data = numbered(self.BIG).encode()
        before = self.fill(data, self.BIG - 3)
        fake = Api()
        fake.error, fake.error_after = DriverError("tmux sessions unknown: tmux failed"), 1
        self.assertEqual(
            self.main_with(fake, "attach"), (1, "", "workers: tmux sessions unknown: tmux failed\n")
        )
        self.assertEqual(
            (self.bytes(), self.read(), os.path.exists(self.old)), (before, data, False)
        )

    def test_attach_after_a_failed_copy_still_numbers_the_new_events(self):
        data = numbered(self.BIG).encode()
        self.fill(data, self.BIG - 3)
        real = os.open

        def open_(path, flags, *args, **kw):
            if path == self.mevents and flags & os.O_APPEND and flags & os.O_NONBLOCK:
                raise OSError(errno.EIO, "Input/output error")
            return real(path, flags, *args, **kw)

        with mock.patch("os.open", open_):
            rc, out, err = self.attach()
        self.assertEqual((rc, out), (0, HEADER + tail(self.mdir, 0, 1)))
        self.assertEqual(
            err,
            f"manager: rotate: {self.mevents}: Input/output error: lines after {self.BIG - 3} of "
            f"{self.old} not copied\n",
        )
        self.assertEqual((self.cursor(), self.read(self.old), self.read()), ((0, 1), data, b""))

    def test_next_event_records_the_line_before_it_waits(self):
        self.fill(numbered(5) + "12:00:00 w6 do")
        self.assertEqual(self.next_event(3), (0, "4 12:00:00 w1 outcome done\n", ""))
        self.assertEqual(self.next_event("end"), (0, "6 12:00:00 w1 outcome done\n", ""))
        self.assertEqual(self.waits, [(self.mevents, 3, 3), (self.mevents, 5, 5)])

    def test_next_event_refuses_stale_line_numbers_on_stdout_writing_nothing(self):
        cases = {
            "past the end": (
                9,
                2,
                f"line 9 is past the end of {self.mevents} (5 lines): run crew.py attach",
            ),
            "an old gen": (3, 1, "stale line numbers (gen 1, now 2): run crew.py attach"),
        }
        for why, (after, gen, msg) in cases.items():
            with self.subTest(why):
                before = self.fill(numbered(5), cursor=2, gen=2)
                self.assertEqual(self.next_event(after, gen), (1, f"workers: {msg}\n", ""))
                self.assertEqual(self.bytes(), before)
        self.assertEqual(self.waits, [])

    def test_next_event_records_then_refuses_when_a_rotation_is_due(self):
        data = numbered(self.BIG).encode()
        self.fill(data)
        self.assertEqual(self.next_event(self.BIG - 3), (1, self.ROTATE, ""))
        self.assertEqual(
            (self.cursor(), self.waits, self.read(), os.path.exists(self.old)),
            ((self.BIG - 3, 0), [], data, False),
        )

    def test_next_event_waits_on_exactly_events_max_bytes(self):
        self.fill(numbered(SPAN))
        self.assertEqual(self.next_event(SPAN), (0, f"{SPAN + 1} 12:00:00 w1 outcome done\n", ""))
        self.assertEqual(self.waits, [(self.mevents, SPAN, SPAN)])

    def test_unhandled_lines_past_events_max_rotate_only_once_half_is_handled(self):
        data = numbered(SPAN + 1).encode()
        self.fill(data)
        half = SPAN // 2
        for k in (1, 2, 3, half - 1):
            with self.subTest(k=k):
                self.assertEqual(self.next_event(k), (0, f"{k + 1} 12:00:00 w1 outcome done\n", ""))
        rc, out, err = self.attach()
        self.assertEqual((rc, err, out.splitlines()[-4]), (0, "", f"N={half - 1} GEN=0"))
        self.assertEqual((self.read(), os.path.exists(self.old)), (data, False))
        self.assertEqual(self.next_event(half), (1, self.ROTATE, ""))
        self.assertEqual((self.cursor(), self.read()), ((half, 0), data))

    def test_next_event_refuses_a_fifo_events_on_stderr(self):
        before = self.seed(MGR)
        os.mkfifo(self.mevents)
        with within():
            self.assertEqual(
                self.next_event(0),
                (1, "", f"workers: {self.mevents}: not a regular file owned by you\n"),
            )
        self.assertEqual(self.bytes(), before)

    def test_an_interrupted_next_event_leaves_its_line_for_the_next_manager(self):
        data = numbered(15)
        self.fill(data)
        with (
            mock.patch.object(self.event_source, "next", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.next_event(12)
        self.assertEqual(
            self.main_with(Api(), "release"), (0, f"workers: released {self.mdir}\n", "")
        )
        backlog = [f"{i} {line}" for i, line in enumerate(data.splitlines()[12:], 13)]
        self.assertEqual(self.attach(own="m2"), (0, HEADER + tail(self.mdir, 12, 0, *backlog), ""))
        self.assertEqual(json.loads(self.bytes())["holder"]["session"], "m2")


if __name__ == "__main__":
    unittest.main()
