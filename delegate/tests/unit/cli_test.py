"""Pure logic over fake objects and in-process adapters."""

import errno
import json
import os
import signal
import types
import uuid
from pathlib import Path
from unittest import mock

from agent_pm.delegate import Begun, Outcome, Result, composer
from agent_pm.delegate.composer.prompt import RESUME
from agent_pm.delegate.engine.runner import STOP_LIMIT
from agent_pm.session import SIGNALS, ConfigError, DriverError

from delegate.tests.case import committed
from delegate.tests.cli import (
    DONE_RESULT,
    DRIVER,
    HEADS,
    KEYS,
    SID,
    SPLIT,
    TUI,
    Base,
    RefusingAdapter,
)
from delegate.tests.replies import block
from delegate.tests.unit.runs import DONE, START, STOP, FakeHandle, no_child, turn
from session.tests import fake_adapter
from session.tests.modules import adapter_loader


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
        self.started.side_effect = lambda *_args, **_kwargs: seen.append(self.driver)
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

        def run(*_args, on_event, **_kwargs):
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
        self.assertEqual((spec.name, kw["events"], spec.sid), (TUI, self.events, SID))
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


class DryRun(Base):
    def test_dry_run_prints_the_plan_and_creates_nothing(self):
        code, out, err = self.runs("--sid", SID, "--dry-run")
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(set(shown), KEYS)
        self.assertEqual(
            {k: v for k, v in shown.items() if k not in ("dirs", "reads", "commands", "prompt")},
            {
                "runtime": "fake",
                "mode": "headless",
                "sid": SID,
                "resume": False,
                "cwd": self.tmp,
                "project": False,
                "tier": 3,
                "effort": "low",
            },
        )
        self.assertEqual(
            (shown["dirs"], shown["reads"], shown["commands"]),
            ([self.work], [os.path.join(composer.TEAM, "tasks")], []),
        )
        self.assertIn("**Your task**: `echo` (`<tasks>/echo.md`).", shown["prompt"])
        self.assertTrue(shown["prompt"].endswith("\n# Input\n\nHello.\n"))
        self.assertEqual(
            err, f"delegate: cwd {self.tmp} is not in trusted_dirs: its project settings are off\n"
        )
        self.assertEqual(
            (self.launches(), os.path.exists(self.work), os.listdir(self.agent_pm)), ([], False, [])
        )

    def test_a_dry_run_configures_the_session(self):
        code, stdout, err = self.runs("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(stdout)["runtime"], "fake")

    def test_a_new_run_gets_a_sid_and_a_resume_continues_its_task(self):
        code, out, _ = self.runs("--dry-run", task=None)
        shown = json.loads(out)
        self.assertEqual((code, uuid.UUID(shown["sid"]).version, shown["resume"]), (0, 4, False))
        self.assertIn("**Your task**: pick it from", shown["prompt"])
        code, out, _ = self.runs("--sid", SID, "--resume", "--dry-run")
        shown = json.loads(out)
        self.assertEqual(
            (code, shown["sid"], shown["resume"], shown["cwd"]), (0, SID, True, self.work)
        )
        self.assertTrue(shown["prompt"].startswith(RESUME))
        self.assertFalse(os.path.exists(self.work))


class InProcess(Base):
    """
    The in_process-only fake, and RefusingAdapter (runtime `refusing`); no child process starts.
    """

    def setUp(self):
        super().setUp()
        refusing = types.ModuleType("agent_pm.session.adapters.refusing")
        refusing.RefusingAdapter = RefusingAdapter
        self.driver_dependencies = {
            "class_loader": adapter_loader(
                {"agent_pm.session.adapters.fake": fake_adapter, refusing.__name__: refusing}
            )
        }
        classes = {"fake": "InProcessFakeAdapter", "refusing": "RefusingAdapter"}
        self.configure_session(
            {
                name: {
                    "implementation": f"agent_pm.session.adapters.{name}:{cls}",
                    "scenario": self.scenario,
                    "log": self.log,
                }
                for name, cls in classes.items()
            }
        )

    def runs(self, *extra, **kw):
        with no_child():
            return super().runs(*extra, **kw)

    def test_a_done_run_exits_0_naming_its_session_and_status(self):
        code, out, err = self.runs("--mode", "in_process", "--sid", SID)
        self.assertEqual((code, out), (0, ""), err)
        lines = err.splitlines()
        self.assertLess(
            lines.index(f"delegate: session {SID}"), lines.index("Progress (start): go")
        )
        self.assertLess(lines.index("Progress (start): go"), lines.index("delegate: status done"))
        self.assertTrue(err.endswith("delegate: status done\n"), err)
        with open(self.out) as f:
            self.assertEqual(f.read(), DONE["deliverable"])
        self.assertEqual(
            [r["kind"] for r in self.records()], ["session", "input", "progress", "result", "end"]
        )
        self.assertEqual([(e["sid"], e["pid"]) for e in self.launches()], [(SID, os.getpid())])

    def test_headless_and_tui_exit_2_and_nothing_runs(self):
        for mode, want in (
            ("headless", "runtime 'fake' does not support headless mode"),
            ("tui", "runtime 'fake' does not support tui mode"),
        ):
            with self.subTest(mode=mode):
                code, out, err = self.runs("--mode", mode)
                self.assertEqual((code, out, err.splitlines()[-1]), (2, "", f"delegate: {want}"))
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_claude_and_codex_refuse_it_with_exit_2(self):
        self.configure_session(committed())
        for runtime in ("claude", "codex"):
            with self.subTest(runtime=runtime):
                code, out, err = self.runs("--mode", "in_process", runtime=runtime)
                self.assertEqual(
                    (code, out, err.splitlines()[-1]),
                    (2, "", f"delegate: runtime {runtime!r} does not support in_process mode"),
                )
                self.assertFalse(os.path.exists(self.work))

    def test_dry_run_shows_the_mode(self):
        code, out, err = self.runs("--mode", "in_process", "--dry-run")
        self.assertEqual((code, json.loads(out)["mode"]), (0, "in_process"), err)
        self.assertFalse(os.path.exists(self.work))

    def test_a_check_refusal_exits_2_with_no_record(self):
        code, out, err = self.runs("--mode", "in_process", runtime="refusing")
        self.assertEqual((code, out, err.splitlines()[-1]), (2, "", "delegate: refused by check"))
        self.assertEqual(
            (os.path.exists(os.path.join(self.work, "run.jsonl")), self.launches()), (False, [])
        )


class Tui(Base):
    def test_tui_mode_runs_the_tui_policy_in_the_drivers_default_session_a_give_up_exiting_1(self):
        for events, want, last in (
            ([turn(START, block(DONE))], 0, "delegate: status done"),
            ([STOP] * (1 + STOP_LIMIT), 1, "delegate: the agent run returned no outcome"),
        ):
            with self.subTest(want=want):
                handle = FakeHandle(events)
                with mock.patch.object(self.driver, "start", side_effect=handle.start) as start:
                    code, out, err = self.runs("--mode", "tui")
                self.assertEqual((code, out, err.splitlines()[-1]), (want, "", last))
                kw = start.call_args.kwargs
                self.assertEqual(
                    (start.call_args.args[:2], start.call_args.args[3].name, kw["events"]),
                    (("fake", "tui"), None, None),
                )

    def test_a_runtime_without_tui_exits_2_and_a_dry_run_plans_it(self):
        code, out, err = self.runs("--mode", "tui")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err.splitlines()[-1], "delegate: runtime 'fake' does not support tui mode")
        self.assertFalse(os.path.exists(self.work))
        code, out, _ = self.runs("--mode", "tui", "--dry-run")
        self.assertEqual((code, json.loads(out)["mode"], self.launches()), (0, "tui", []))


class List(Base):
    def test_list_prints_each_role_and_task_with_its_source(self):
        code, out, err = self.cli("--list")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual([line for line in lines if not line.startswith("  ")], HEADS)
        echo = composer.index("dummy-tester")["echo"]
        self.assertTrue(
            out.endswith(f"\ndummy-tester  local  built-in\n  echo: {echo}  [built-in]\n"), out
        )
        self.assertTrue(
            all(line.endswith("  [built-in]") for line in lines if line.startswith("  "))
        )
        self.assertNotIn("<tasks>", out)
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_list_abbreviated_is_list_and_takes_no_other_option(self):
        self.assertEqual(self.cli("--li"), self.cli("--list"))
        for argv, extra in (
            (["--list", "--role", "pm"], "--role"),
            (["--li", "--rol", "pm"], "--rol"),
            (["--list", "--dry-run"], "--dry-run"),
            (["--list", "--runtime=fake"], "--runtime=fake"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"--list takes no other option, not {extra}\n", err)
        code, out, err = self.cli()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("the following arguments are required: --role", err)

    def test_list_stops_at_a_config_defect(self):
        for local, want in (
            (
                "[roles.ghost]\ntier = 3\n",
                "roles.ghost: no charter roles/ghost.md in core or team_dirs",
            ),
            ('team_dirs = "x"\n', "team_dirs: want a list of absolute or ~ paths, got 'x'"),
        ):
            with self.subTest(local=local):
                self.local(local)
                self.assertEqual(self.cli("--list"), (2, "", f"delegate: {want}\n"))


class Configures(Base):
    def test_a_session_config_error_exits_2_and_starts_nothing(self):
        self.configure_session({})
        code, out, err = self.runs()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("[adapters.fake]", err)
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_an_unknown_harness_host_or_delegates_key_exits_2_and_starts_nothing(self):
        self.application = None
        for text, want in (
            (
                '[delegates]\nharness_host = "nope"\n',
                "delegate: delegate config: [delegates] harness_host: want one of tmux",
            ),
            (
                "[delegates]\nkind = 1\n",
                "delegate: delegate config: [delegates] kind: the delegate takes only harness_host",
            ),
        ):
            with self.subTest(text):
                self.local(text)
                code, out, err = self.runs()
                self.assertEqual((code, out, err), (2, "", f"{want}\n"))
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_list_works_without_a_runtime(self):
        self.configure_session({})
        code, _, err = self.cli("--list")
        self.assertEqual((code, err), (0, ""))


class Usage(Base):
    def test_a_run_needs_input_out_and_workdir(self):
        for argv in (
            ("--out", "o.md"),
            ("--input", "x", "--workdir", self.work),
            ("--input", "x", "--out", "o.md"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(
                    self.cli("--role", "dummy-tester", "--runtime", "fake", *argv),
                    (2, "", "delegate: --input, --out and --workdir are required\n"),
                )
        self.assertEqual(self.launches(), [])

    def test_resume_needs_sid(self):
        self.assertEqual(self.runs("--resume"), (2, "", "delegate: --resume needs --sid\n"))
        self.assertEqual(self.launches(), [])

    @mock.patch.dict(os.environ, {"NO_COLOR": "1"})
    def test_an_unknown_mode_exits_2_naming_each_mode(self):
        code, out, err = self.runs("--mode", "x")
        self.assertEqual((code, out), (2, ""))
        self.assertIn(
            "argument --mode: invalid choice: 'x' (choose from 'headless', 'tui', 'in_process')",
            " ".join(err.split()),
        )
        self.assertEqual(self.launches(), [])

    def test_runtime_given_twice_exits_2(self):
        for extra in (("--runtime", "fake"), ("--runtime=fake",)):
            with self.subTest(extra=extra):
                code, out, err = self.runs(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--runtime given twice", err)
        self.assertEqual(self.launches(), [])

    def test_out_or_workdir_given_twice_exits_2_before_anything_starts(self):
        for extra, error in (
            (("--out", "b.md"), "--out given twice"),
            (("--workdir", "b"), "--workdir given twice"),
        ):
            with self.subTest(extra=extra):
                code, out, err = self.runs(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(error, err)
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_a_config_error_exits_2_before_anything_starts(self):
        cases = [
            (
                ("an unknown role"),
                {"role": "ghost", "task": None},
                (""),
                ("unknown role 'ghost'"),
            ),
            (
                "a task off the index",
                {"role": "engineer", "task": "deep-research"},
                "",
                "task 'deep-research' is not one of engineer's tasks (build, light-build)",
            ),
            (
                "a bad team_dirs",
                {},
                'team_dirs = "x"\n',
                "team_dirs: want a list of absolute or ~ paths, got 'x'",
            ),
            (
                "a bad tier",
                {},
                "[roles.dummy-tester]\ntier = 9\n",
                "tier must be an integer 1–4, got 9",
            ),
            ("an unknown runtime", {"runtime": "nope"}, "", "no [adapters.nope] table"),
        ]
        for name, kw, local, want in cases:
            self.local(local)
            for dry in ((), ("--dry-run",)) if name != "an unknown runtime" else ((),):
                with self.subTest(name, dry=dry):
                    code, out, err = self.runs(*dry, **kw)
                    self.assertEqual((code, out), (2, ""), err)
                    self.assertTrue(err.splitlines()[-1].startswith(f"delegate: {want}"), err)
                    self.assertFalse(os.path.exists(self.work))
        self.assertEqual(self.launches(), [])
