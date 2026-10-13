"""Detach behavior."""

import errno
import json
import os
import signal
from pathlib import Path
from unittest import mock

from agent_pm.delegate import Begun, Outcome, Result
from agent_pm.delegate.engine.runner import STOP_LIMIT
from agent_pm.session import SIGNALS, ConfigError, DriverError

from delegate.tests.unit.cli.case import DONE_RESULT, DRIVER, KEYS, SID, SPLIT, TUI, Base
from delegate.tests.unit.runs import STOP, FakeHandle


class Detach(Base):
    """detach_session and layout (the opener `mgr`) patched; events file `events`."""

    def setUp(self):
        super().setUp()
        self.events = os.path.join(self.tmp, "events")
        self.started = self.enterContext(mock.patch.object(self.driver, "detach"))
        self.layout = self.enterContext(
            mock.patch.object(self.driver, "layout", return_value="mgr")
        )

    def detaches(self, *extra, events=None, **kw):
        return self.runs("--sid", SID, "--detach", "--events", events or self.events, *extra, **kw)

    def assert_nothing_started(self):
        self.started.assert_not_called()
        self.assertEqual(
            (self.launches(), os.path.exists(self.work), os.path.exists(self.events)),
            ([], False, False),
        )

    def test_usage_errors_exit_2_and_start_nothing(self):
        e = self.events
        for argv, out, want in (
            (("--detach",), None, "--detach needs --events"),
            (("--events", e), None, "--events needs --detach"),
            (("--events", e, "--mode", "tui"), None, "--events needs --detach"),
            (("--split", "right", "--mode", "tui"), None, SPLIT),
            (("--split-from", "mgr"), None, SPLIT),
            (("--detach", "--events", e, "--split", "below"), None, SPLIT),
            (
                ("--detach", "--events", e, "--split-from", "mgr", "--mode", "in_process"),
                None,
                SPLIT,
            ),
            (("--detach", "--events", e), "-", "--detach needs --out PATH"),
            (("--detach", "--events", e, "--dry-run"), "-", "--detach needs --out PATH"),
        ):
            with self.subTest(argv=argv, out=out):
                self.assertEqual(self.runs(*argv, out=out), (2, "", f"delegate: {want}\n"))
        self.layout.assert_not_called()
        self.assert_nothing_started()

    def test_detach_refusals_exit_2_with_nothing_started(self):
        folder = os.path.join(self.tmp, "dir")
        os.mkdir(folder)
        for extra, events, want in (
            (
                ("--mode", "in_process"),
                None,
                "--detach: mode must be headless or tui, got 'in_process'",
            ),
            ((), folder, f"events file {folder}: {os.strerror(errno.EISDIR)}"),
        ):
            with self.subTest(want=want):
                code, out, err = self.detaches(*extra, events=events)
                self.assertEqual((code, out, err.splitlines()[-1]), (2, "", f"delegate: {want}"))
        self.assertEqual(os.listdir(folder), [])
        self.assert_nothing_started()

    def test_dry_run_adds_the_session_names_and_the_events_file(self):
        for mode, tui in (("headless", None), ("tui", TUI)):
            with self.subTest(mode=mode):
                code, out, err = self.detaches("--mode", mode, "--dry-run", events="events")
                self.assertEqual(code, 0, err)
                shown = json.loads(out)
                self.assertEqual(set(shown), KEYS | {"driver", "tui", "events"})
                self.assertEqual(
                    (shown["mode"], shown["driver"], shown["tui"], shown["events"]),
                    (mode, DRIVER, tui, self.events),
                )
        self.layout.assert_not_called()
        self.assert_nothing_started()

    def test_detach_names_its_sessions_and_exits_0_at_once(self):
        lines = [
            f"delegate: session {SID}",
            f"delegate: driver session {DRIVER}: {self.driver.attach_command(DRIVER)}",
            f"delegate: tui session {TUI}: {self.driver.attach_command(TUI)}",
        ]
        for mode, extra, shown, tail in (
            ("headless", (), lines[:2], [f"--events={self.events}", "--task=echo"]),
            (
                "tui",
                ("--split", "below", "--split-from", "mgr"),
                lines,
                ["--split=below", "--split-from=mgr", "--opener=mgr"],
            ),
        ):
            with self.subTest(mode=mode):
                code, out, err = self.detaches("--mode", mode, *extra)
                self.assertEqual((code, out, err.splitlines()[1:]), (0, "", shown), err)
                name, argv = self.started.call_args.args
                self.assertEqual((name, argv[-len(tail) :]), (DRIVER, tail))
        self.layout.assert_called_once_with("below", "mgr")
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_detach_configures_the_session_before_reaching_it(self):
        seen = []
        self.started.side_effect = lambda *a, **kw: seen.append(self.driver)
        code, _, err = self.detaches()
        self.assertEqual((code, len(seen)), (0, 1), err)
        self.assertIs(seen[0], self.driver)

    def test_input_dash_is_read_by_the_caller(self):
        code, _, err = self.detaches(input="-", stdin="-x\nUse B.\n")
        self.assertEqual(code, 0, err)
        self.assertIn("--input=-x\nUse B.\n", self.started.call_args.args[1])

    def test_a_driver_failure_exits_3(self):
        self.started.side_effect = DriverError(f"detach {DRIVER}: duplicate session")
        code, out, err = self.detaches()
        self.assertEqual(
            (code, out, err.splitlines()[1:]),
            (3, "", [f"delegate: detach {DRIVER}: duplicate session"]),
        )


class DriverForm(Base):
    """The driver form (--driver) with events file `events` and show patched; signal.signal and
    signal.pthread_sigmask record their calls in self.calls, the handler in self.handler.
    """

    def setUp(self):
        super().setUp()
        self.events = os.path.join(self.tmp, "events")
        self.calls, self.handler = [], None
        self.show = self.enterContext(mock.patch.object(self.driver, "show", return_value=None))
        self.enterContext(mock.patch.object(signal, "signal", side_effect=self.installed))
        self.enterContext(mock.patch.object(signal, "pthread_sigmask", side_effect=self.masked))

    def installed(self, signum, handler):
        self.calls.append(("signal", signum))
        self.handler = handler

    def masked(self, how, signals):
        self.calls.append((how, tuple(signals), self.written()))

    def written(self) -> str:
        if not os.path.exists(self.events):
            return ""
        with open(self.events) as f:
            return f.read()

    def drives(self, *extra, mode="headless", **kw):
        Path(self.events).unlink(missing_ok=True)
        return self.runs(
            "--driver", DRIVER, "--events", self.events, "--sid", SID, "--mode", mode, *extra, **kw
        )

    def assert_outcome(self, status):
        self.assertRegex(self.written(), rf"\A\d\d:\d\d:\d\d {DRIVER} outcome {status}\n\Z")

    def ran(self, result=DONE_RESULT, effect=None):
        """the fake application: Begun, then `effect` (raised when an exception, else called) and
        `result`; noted in self.calls.
        """

        def run(*args, on_event, **kw):
            self.calls.append(("run",))
            on_event(Begun(SID))
            if isinstance(effect, BaseException):
                raise effect
            if effect:
                effect()
            return result

        return mock.patch.object(self.application, "run", side_effect=run)

    def test_the_driver_form_configures_the_session_before_its_run(self):
        seen = []
        with self.ran(effect=lambda: seen.append(self.driver)):
            code, _, err = self.drives()
        self.assertEqual((code, len(seen)), (0, 1), err)
        self.assertIs(seen[0], self.driver)

    def test_the_handler_on_each_signal_unblocked_for_the_run_blocked_for_the_outcome_line(self):
        with self.ran():
            code, out, err = self.drives()
        self.assertEqual((code, out), (0, ""), err)
        self.assertEqual(
            self.calls,
            [
                ("signal", signal.SIGHUP),
                ("signal", signal.SIGTERM),
                ("signal", signal.SIGINT),
                (signal.SIG_UNBLOCK, SIGNALS, ""),
                ("run",),
                (signal.SIG_BLOCK, SIGNALS, ""),
            ],
        )
        self.assert_outcome("done")

    def test_exit_codes_and_the_one_outcome_line(self):
        for name, result, want, status in (
            ("done", DONE_RESULT, 0, "done"),
            ("needs_input", Result(0, Outcome("needs_input", "t", "s", ["Q?"])), 0, "needs_input"),
            ("failed", Result(0, Outcome("failed", "t", "s")), 0, "failed"),
            ("no outcome", Result(0, None, "the agent run returned no outcome"), 1, "error"),
            ("the client exited 5", Result(5, None, "the client exited 5"), 3, "error"),
            ("no start", Result(1, None, "headless: cannot start x"), 3, "error"),
        ):
            with self.subTest(name), self.ran(result) as run:
                code, out, err = self.drives()
                self.assertEqual((code, out), (want, ""), err)
                self.assert_outcome(status)
                spec = run.call_args.args[1]
                self.assertEqual((spec.sid, spec.name, spec.events), (SID, None, None))

    def test_an_exception_writes_error_and_propagates(self):
        with self.ran(effect=RuntimeError("boom")), self.assertRaisesRegex(RuntimeError, "boom"):
            self.drives()
        self.assert_outcome("error")

    def test_a_signal_blocks_them_all_writes_error_and_exits_128_plus_signum(self):
        with self.ran(effect=lambda: self.handler(signal.SIGTERM, None)):
            code, out, _ = self.drives()
        self.assertEqual((code, out), (128 + signal.SIGTERM, ""))
        block = (signal.SIG_BLOCK, SIGNALS, "")
        self.assertEqual(self.calls[-3:], [("run",), block, block])
        self.assert_outcome("error")

    def test_a_config_error_exits_2_and_writes_error(self):
        for kw, extra, want in (
            ({"role": "ghost", "task": None}, (), "unknown role 'ghost'"),
            ({}, ("--split", "right"), SPLIT),
        ):
            with self.subTest(want=want), self.ran() as run:
                code, out, err = self.drives(*extra, **kw)
                self.assertEqual((code, out, err.splitlines()[-1]), (2, "", f"delegate: {want}"))
                run.assert_not_called()
                self.assert_outcome("error")

    def test_a_tui_run_in_its_named_session_given_up_and_left_open_writes_error_exits_1(self):
        handle = FakeHandle([STOP] * (1 + STOP_LIMIT))
        with mock.patch.object(self.driver, "start", side_effect=handle.start) as start:
            code, out, err = self.drives(mode="tui")
        self.assertEqual(
            (code, out, err.splitlines()[-1]),
            (1, "", "delegate: the agent run returned no outcome"),
        )
        spec, kw = start.call_args.args[3], start.call_args.kwargs
        self.assertEqual((spec.name, kw["options"].events, spec.sid), (TUI, self.events, SID))
        self.assertIn(f"delegate: tui session {TUI}: {self.driver.attach_command(TUI)}", err)
        self.show.assert_not_called()
        self.assert_outcome("error")

    def test_input_dash_is_the_literal_text_stdin_unread(self):
        with self.ran() as run:
            code, _, err = self.drives(input="-", stdin="SECRET")
        self.assertEqual((code, run.call_args.args[1].input), (0, "-"), err)

    def test_begun_shows_the_tui_beside_its_opener_its_errors_printed_the_run_going_on(self):
        command = "tmux attach -t '=x'"
        for name, returned, raised, printed in (
            ("a pane", None, None, []),
            ("an attach command", command, None, [command]),
            (
                "a DriverError",
                None,
                DriverError("no tmux session x"),
                [f"delegate: show {TUI}: no tmux session x"],
            ),
            (
                "a ConfigError",
                None,
                ConfigError("bad opener"),
                [f"delegate: show {TUI}: bad opener"],
            ),
        ):
            with self.subTest(name), self.ran():
                self.show.reset_mock(return_value=True, side_effect=True)
                self.show.return_value, self.show.side_effect = returned, raised
                code, out, err = self.drives(
                    "--split", "below", "--split-from", "mgr", "--opener", "mgr", mode="tui"
                )
                self.assertEqual((code, out), (0, ""), err)
                self.show.assert_called_once_with(TUI, "below", "mgr", "mgr")
                lines = err.splitlines()
                at = lines.index(f"delegate: session {SID}")
                self.assertEqual(lines[at + 1 :], [*printed, "delegate: status done"])
                self.assert_outcome("done")

    def test_no_pane_without_an_opener_or_outside_tui(self):
        for mode, extra in (("tui", ()), ("headless", ("--opener", "mgr"))):
            with self.subTest(mode=mode), self.ran():
                code, _, err = self.drives(*extra, mode=mode)
                self.assertEqual(code, 0, err)
        self.show.assert_not_called()

    def test_the_driver_form_needs_events_and_sid(self):
        for extra in (("--events", self.events), ("--sid", SID)):
            with self.subTest(extra=extra):
                code, out, err = self.runs("--driver", DRIVER, *extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--driver needs --events and --sid\n", err)
        self.assertEqual((self.calls, self.written()), ([], ""))
