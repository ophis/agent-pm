"""Restart behavior."""

import contextlib
import io
import json
import os
import stat
import sys
from unittest import mock

from agent_pm.manager.engine import crew, roster
from agent_pm.manager.engine.types import NotAWorker
from agent_pm.session import (
    Activity,
    DriverError,
    Exited,
    IdleTimeout,
)
from agent_pm.utils import save

from manager.tests.case import PYTHONPATH
from manager.tests.unit.crew.case import RosterCase, WorkerCase
from manager.tests.unit.crew.data import (
    CREW,
    DEAD_TEXT,
    MGR,
    MODULE,
    OTHER,
    SID,
    SID2,
    WHEN,
    SessionView,
    entry,
    fixed_time,
    lock_free,
    recovered,
    row,
    start,
    tail,
    within,
)
from manager.tests.unit.crew.runtime import Api


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
        with self.assertRaises(NotAWorker) as cm:
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
            "missing": lambda _path: None,
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
        self.tamper(lambda path: save(path, json.dumps(self.record(SID, env={"A-B": "x"}))))
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
            f"manager: entry w1 not written: manager directory {self.directory}: "
            "held by tmux session "
            f"other since {WHEN}; ask that manager to run crew.py release, or end that session\n",
            err.getvalue(),
        )
        self.assertEqual(len(self.api.starts), 2)


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
        def unknown(_name):
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
        rc, _, err = self.main_with(
            Api(rows=[row("w1", sid, view=SessionView(state="dead", shown="mgr"))]), "attach"
        )
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
        def take_over(_name):
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
