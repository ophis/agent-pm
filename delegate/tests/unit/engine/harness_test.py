"""The detached harness preserves signal, display and outcome lifecycle behavior."""

import hashlib
import io
import json
import os
import signal
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_pm.delegate import Begun, Outcome, Result
from agent_pm.delegate.engine import detach, harness
from agent_pm.delegate.engine.runner import STOP_LIMIT
from agent_pm.session import SIGNALS, ConfigError, DriverError

from delegate.tests.application_case import DRIVER, SID, TUI, Base
from delegate.tests.unit.runs import STOP, FakeHandle

DONE_RESULT = Result(0, Outcome("done", "t", "s"))


class Harness(Base):
    """The harness with events file `events` and show patched; signal.signal and
    signal.pthread_sigmask record their calls in self.calls, the handler in self.handler.
    """

    def setUp(self):
        super().setUp()
        self.configure_fake(tui=True)
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
        invocation = self.given(mode=mode, **kw)
        invocation.update(
            {
                key.removeprefix("--").replace("-", "_"): value
                for key, value in zip(extra[::2], extra[1::2], strict=True)
            }
        )
        _, environment = self.state(invocation)
        return self.execute(environment, stdin=stdin)

    def given(self, **kw):
        invocation = {
            "role": "dummy-tester",
            "task": "echo",
            "input": "Hello.",
            "out": self.out,
            "workdir": self.work,
            "runtime": "fake",
            "mode": "headless",
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
        invocation.setdefault(
            "harness", detach.names(invocation["role"], invocation["sid"], invocation["mode"])[1]
        )
        return invocation

    def state(self, invocation):
        os.makedirs(self.work, exist_ok=True)
        sid_hash = hashlib.sha256(invocation["sid"].encode("utf-8")).hexdigest()
        descriptor, filename = tempfile.mkstemp(
            dir=self.work, prefix=f"harness-{sid_hash}-", suffix=".json"
        )
        payload = json.dumps(invocation).encode("utf-8")
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        return Path(filename), {
            "AGENT_PM_HARNESS_STATE": os.path.basename(filename),
            "AGENT_PM_HARNESS_DIGEST": hashlib.sha256(payload).hexdigest(),
        }

    def execute(self, environment, *, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch("sys.stdin", io.StringIO(stdin)),
            mock.patch.dict(os.environ, environment),
        ):
            code = harness.main([self.work], application=self.application)
        return code, out.getvalue(), err.getvalue()

    def assert_outcome(self, status, name=DRIVER):
        self.assertRegex(self.written(), rf"\A\d\d:\d\d:\d\d {name} outcome {status}\n\Z")

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
            ("the client exited 5", Result(5, None, "the client exited 5"), 1, "error"),
            ("no start", Result(1, None, "headless: cannot start x"), 1, "error"),
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
        for kw, want in (({"role": "ghost", "task": None}, "unknown role 'ghost'"),):
            with self.subTest(want=want), self.ran() as run:
                code, out, err = self.drives(**kw)
                self.assertEqual((code, out, err.splitlines()[-1]), (2, "", f"delegate: {want}"))
                run.assert_not_called()
                name = detach.names(kw.get("role", "dummy-tester"), SID, "headless")[1]
                self.assert_outcome("error", name)

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

    def test_anything_but_one_workdir_argument_exits_2_with_its_usage(self):
        for argv in ([], [self.work, self.work]):
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(argv=argv), redirect_stdout(out), redirect_stderr(err):
                code = harness.main(argv, application=self.application)
            self.assertEqual((code, out.getvalue()), (2, ""))
            self.assertEqual(err.getvalue(), "usage: delegate harness WORKDIR\n")
        self.assertEqual((self.calls, self.written()), ([], ""))

    def assert_refused(self, environment):
        with self.ran() as run:
            code, out, err = self.execute(environment)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("delegate: harness state:", err)
        self.assertEqual((self.calls, self.written()), ([], ""))
        run.assert_not_called()

    def test_validated_state_is_consumed_and_credentials_do_not_reach_the_application(self):
        path, environment = self.state(self.given())
        seen = []
        with self.ran(
            effect=lambda: seen.append((path.exists(), set(environment).intersection(os.environ)))
        ):
            code, out, err = self.execute(environment)
        self.assertEqual((code, out, seen), (0, "", [(False, set())]), err)
        self.assert_outcome("done")

    def test_tampered_role_runtime_events_and_output_are_refused_even_as_valid_json(self):
        for changes in (
            {"role": "engineer"},
            {"runtime": "claude"},
            {"events": os.path.join(self.tmp, "forged-events")},
            {"out": os.path.join(self.tmp, "forged-output")},
        ):
            with self.subTest(changes=changes):
                given = self.given()
                path, environment = self.state(given)
                path.write_text(json.dumps(given | changes), encoding="utf-8")
                self.assert_refused(environment)
                self.assertTrue(path.exists())
        self.assertFalse(Path(self.tmp, "forged-events").exists())
        self.assertFalse(Path(self.tmp, "forged-output").exists())

    def test_authenticated_but_malformed_state_is_refused_before_any_outcome(self):
        given = self.given()
        malformed = [
            b"{",
            b"\xff",
            b"[]",
            json.dumps({key: value for key, value in given.items() if key != "runtime"}).encode(),
        ]
        for changes in (
            {"extra": True},
            {"resume": 1},
            {"detach": True},
            {"dry_run": True},
            {"list": True},
            {"sid": "bad/name"},
            {"sid": "other-session"},
            {"role": "ghost\n"},
            {"runtime": ""},
            {"mode": []},
            {"input": None},
            {"input": "unsafe\0input"},
            {"events": "relative"},
            {"out": self.out + "\n"},
            {"workdir": self.tmp},
            {"task": 1},
            {"repo": ""},
            {"split": 1},
            {"split_from": "bad\n"},
            {"opener": []},
            {"harness": "forged-drive"},
        ):
            malformed.append(json.dumps(given | changes).encode())
        for payload in malformed:
            with self.subTest(payload=payload):
                path, environment = self.state(given)
                path.write_bytes(payload)
                environment["AGENT_PM_HARNESS_DIGEST"] = hashlib.sha256(payload).hexdigest()
                self.assert_refused(environment)

    def test_launch_credentials_cannot_select_another_file(self):
        _, trusted = self.state(self.given())
        for changes in (
            {"AGENT_PM_HARNESS_STATE": "../harness.json"},
            {"AGENT_PM_HARNESS_STATE": os.path.join(self.work, trusted["AGENT_PM_HARNESS_STATE"])},
            {"AGENT_PM_HARNESS_STATE": ""},
            {"AGENT_PM_HARNESS_DIGEST": ""},
            {"AGENT_PM_HARNESS_DIGEST": "A" * 64},
        ):
            with self.subTest(changes=changes):
                self.assert_refused(trusted | changes)

    def test_an_invocation_symlink_is_refused(self):
        path, environment = self.state(self.given())
        target = path.with_name("actual-state.json")
        path.rename(target)
        path.symlink_to(target)
        self.assert_refused(environment)
        self.assertTrue(target.exists())

    def test_an_invocation_with_nonprivate_permissions_is_refused(self):
        path, environment = self.state(self.given())
        path.chmod(0o644)
        self.assert_refused(environment)

    def test_an_invocation_owned_by_another_user_is_refused(self):
        _, environment = self.state(self.given())
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.assert_refused(environment)

    def test_an_invocation_must_be_a_regular_file(self):
        for kind in ("directory", "fifo"):
            with self.subTest(kind=kind):
                path, environment = self.state(self.given())
                path.unlink()
                if kind == "directory":
                    path.mkdir(mode=0o600)
                else:
                    os.mkfifo(path, mode=0o600)
                self.assert_refused(environment)
