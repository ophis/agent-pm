"""The detached harness preserves signal, display and outcome lifecycle behavior."""

import io
import json
import os
import signal
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_pm.delegate import Begun, Outcome, Result
from agent_pm.delegate.engine import harness
from agent_pm.delegate.engine.runner import STOP_LIMIT
from agent_pm.session import SIGNALS, ConfigError, DriverError

from delegate.tests.unit.engine.application_case import DRIVER, SID, SPLIT, TUI, Base
from delegate.tests.unit.runs import STOP, FakeHandle

DONE_RESULT = Result(0, Outcome("done", "t", "s"))


class Harness(Base):
    """The harness with events file `events` and show patched; signal.signal and
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
        stdin = kw.pop("stdin", "")
        invocation = {
            "harness": DRIVER,
            "role": "dummy-tester",
            "task": "echo",
            "input": "Hello.",
            "out": self.out,
            "workdir": self.work,
            "runtime": "fake",
            "mode": mode,
            "sid": SID,
            "events": self.events,
            "split": None,
            "split_from": None,
            "opener": None,
            "repo": None,
            "resume": False,
            "detach": False,
            "dry_run": False,
            "list": False,
            **kw,
        }
        invocation.update(
            {
                key.removeprefix("--").replace("-", "_"): value
                for key, value in zip(extra[::2], extra[1::2], strict=True)
            }
        )
        os.makedirs(self.work, exist_ok=True)
        Path(self.work, "harness.json").write_text(json.dumps(invocation))
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch("sys.stdin", io.StringIO(stdin)),
        ):
            code = harness.main([self.work], application=self.application)
        return code, out.getvalue(), err.getvalue()

    def assert_outcome(self, status):
        self.assertRegex(self.written(), rf"\A\d\d:\d\d:\d\d {DRIVER} outcome {status}\n\Z")

    def ran(self, result=DONE_RESULT, effect=None):
        """the fake application: Begun, then `effect` (raised when an exception, else called) and
        `result`; noted in self.calls.
        """

        def run(*_args, on_event, **_kwargs):
            self.calls.append(("run",))
            on_event(Begun(SID))
            if isinstance(effect, BaseException):
                raise effect
            if effect:
                effect()
            return result

        return mock.patch.object(self.application, "run", side_effect=run)

    def test_the_harness_configures_the_session_before_its_run(self):
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
        spec = start.call_args.args[0].session
        self.assertEqual((spec.name, spec.events, spec.sid), (TUI, self.events, SID))
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

    def test_missing_harness_state_exits_2_before_signal_handlers_or_an_outcome(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = harness.main([self.work], application=self.application)
        self.assertEqual((code, out.getvalue()), (2, ""))
        self.assertIn("delegate: harness state:", err.getvalue())
        self.assertEqual((self.calls, self.written()), ([], ""))
