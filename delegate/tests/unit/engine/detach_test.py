"""delegate/engine/detach.py: names; detach's call of detach_session (argv, env, cwd, opener), its
refusals with nothing started and the Driver's errors; outcome's line and its write errors."""

import errno
import io
import os
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_pm.delegate import ConfigError
from agent_pm.delegate.engine import detach
from agent_pm.delegate.engine.config import PYTHONPATH
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import DriverError

from delegate.tests.case import Case
from delegate.tests.unit.engine.plans import SID, Base

TUI = "dummy-tester-11111111"
DRIVER = f"{TUI}-drive"
INPUT = "-x: an input led by a dash"
TUI_ONLY = "split and split_from: tui mode only"


class Names(Case):
    def test_tui_mode_names_both_sessions_else_only_the_driver(self):
        self.assertEqual(detach.names("dummy-tester", SID, "tui"), (TUI, DRIVER))
        self.assertEqual(detach.names("dummy-tester", SID, "headless"), (None, DRIVER))


class Detach(Base):
    """The fake driver's detach and layout recorded (the opener `mgr`); the cwd self.tmp."""

    def setUp(self):
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        self.events = os.path.join(self.tmp, "events")
        self.started = self.enterContext(mock.patch.object(self.driver, "detach"))
        self.layout = self.enterContext(
            mock.patch.object(self.driver, "layout", return_value="mgr")
        )

    def plan(self, mode="headless", task="echo", **kw):
        return self.planned("dummy-tester", task, mode=mode, input=INPUT, sid=SID, **kw)[0]

    def detached(self, p, events=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            got = detach.detach(p, events=events or self.events, **kw)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        return got

    def argv(self, mode, *extra, task="echo"):
        return [
            sys.executable,
            "-P",
            "-m",
            "agent_pm.delegate.cli",
            f"--driver={DRIVER}",
            "--role=dummy-tester",
            f"--input={INPUT}",
            f"--out={self.out}",
            f"--workdir={self.work}",
            "--runtime=fake",
            f"--mode={mode}",
            f"--sid={SID}",
            f"--events={self.events}",
            *((f"--task={task}",) if task else ()),
            *extra,
        ]

    def refusal(self, p, **kw) -> str:
        with self.assertRaises(ConfigError) as cm:
            self.detached(p, **kw)
        self.started.assert_not_called()
        return str(cm.exception)

    def test_headless_runs_the_driver_form_with_the_import_root_and_the_cwd(self):
        self.assertEqual(self.detached(self.plan(), "events"), detach.Detached(DRIVER, None))
        self.layout.assert_not_called()
        self.started.assert_called_once_with(
            DRIVER,
            self.argv("headless"),
            cwd=self.tmp,
            env={**os.environ, "PYTHONPATH": PYTHONPATH},
            opener=None,
        )
        with open(self.events, "rb") as f:
            self.assertEqual(f.read(), b"")

    def test_tui_passes_each_option_given_and_the_layouts_opener(self):
        got = self.detached(self.plan("tui", repo="-r"), split="right", split_from="mgr")
        self.assertEqual(got, detach.Detached(DRIVER, TUI))
        self.layout.assert_called_once_with("right", "mgr")
        self.assertEqual(
            self.started.call_args.args[1],
            self.argv("tui", "--repo=-r", "--split=right", "--split-from=mgr", "--opener=mgr"),
        )
        self.assertEqual(self.started.call_args.kwargs["opener"], "mgr")

    def test_task_only_when_set_resume_only_on_a_resume(self):
        self.detached(self.plan(task=None))
        self.detached(self.plan(resume=True))
        self.assertEqual(
            [c.args[1] for c in self.started.call_args_list],
            [self.argv("headless", task=None), self.argv("headless", "--resume")],
        )

    def test_no_pane_without_split_from_is_no_opener(self):
        self.layout.side_effect = DriverError("no pane to show the TUI beside (x)")
        self.assertEqual(
            self.detached(self.plan("tui"), split="below"), detach.Detached(DRIVER, TUI)
        )
        self.assertEqual(self.started.call_args.args[1], self.argv("tui", "--split=below"))
        self.assertIsNone(self.started.call_args.kwargs["opener"])

    def test_layout_errors_with_split_from_are_refusals(self):
        for error in (DriverError("no pane (x)"), SessionConfigError("no tmux session mgr")):
            with self.subTest(error=error):
                self.layout.side_effect = error
                self.assertEqual(self.refusal(self.plan("tui"), split_from="mgr"), str(error))

    def test_mode_and_split_refusals_start_and_write_nothing(self):
        for p, kw, want in (
            (
                self.plan("in_process"),
                {},
                "--detach: mode must be headless or tui, got 'in_process'",
            ),
            (self.plan(), {"split": "right"}, TUI_ONLY),
            (self.plan(), {"split_from": "mgr"}, TUI_ONLY),
        ):
            with self.subTest(mode=p.spec.mode, kw=kw):
                self.assertEqual(self.refusal(p, **kw), want)
        self.layout.assert_not_called()
        self.assertFalse(os.path.exists(self.events))

    def test_an_unusable_events_file_is_refused(self):
        target, link, folder, theirs = (
            os.path.join(self.tmp, n) for n in ("target", "link", "dir", "theirs")
        )
        os.symlink(target, link)
        os.mkdir(folder)
        with open(theirs, "w") as f:
            f.write("keep\n")
        p, uid = self.plan("tui"), os.getuid()
        for events, getuid, why in (
            (link, uid, os.strerror(errno.ELOOP)),
            (folder, uid, os.strerror(errno.EISDIR)),
            (theirs, uid + 1, "not owned by you"),
        ):
            with self.subTest(events=events), mock.patch.object(os, "getuid", return_value=getuid):
                self.assertEqual(self.refusal(p, events=events), f"events file {events}: {why}")
        self.layout.assert_not_called()
        self.assertFalse(os.path.lexists(target))
        self.assertEqual(os.listdir(folder), [])
        with open(theirs) as f:
            self.assertEqual(f.read(), "keep\n")

    def test_the_drivers_refusal_is_a_config_error_its_failure_propagates(self):
        for error, raised in (
            (SessionConfigError("cwd must be an existing directory"), ConfigError),
            (DriverError(f"detach {DRIVER}: duplicate session"), DriverError),
        ):
            with self.subTest(error=error):
                self.started.side_effect = error
                with self.assertRaises(raised) as cm:
                    self.detached(self.plan())
                self.assertEqual(str(cm.exception), str(error))


class Outcome(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.events = os.path.join(self.tmp, "events")

    def outcome(self, events, status) -> str:
        err = io.StringIO()
        with (
            redirect_stderr(err),
            mock.patch.object(time, "strftime", return_value="12:34:56") as clock,
        ):
            detach.outcome(events, DRIVER, status)
        clock.assert_called_once_with("%H:%M:%S")
        return err.getvalue()

    def test_appends_the_outcome_line_none_as_error(self):
        for status in ("done", "needs_input", None):
            self.assertEqual(self.outcome(self.events, status), "")
        with open(self.events) as f:
            self.assertEqual(
                f.read(),
                "".join(
                    f"12:34:56 {DRIVER} outcome {s}\n" for s in ("done", "needs_input", "error")
                ),
            )

    def test_a_write_error_is_printed_not_raised_and_nothing_written(self):
        folder = os.path.join(self.tmp, "dir")
        os.mkdir(folder)
        error = OSError(errno.EISDIR, os.strerror(errno.EISDIR), folder)
        self.assertEqual(self.outcome(folder, "done"), f"delegate: {error}\n")
        self.assertEqual(os.listdir(folder), [])
        with open(self.events, "w") as f:
            f.write("keep\n")
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            err = self.outcome(self.events, "done")
        self.assertEqual(
            err, f"delegate: {OSError(errno.EPERM, 'not owned by you', self.events)}\n"
        )
        with open(self.events) as f:
            self.assertEqual(f.read(), "keep\n")
