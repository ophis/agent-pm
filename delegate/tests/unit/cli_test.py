"""Pure logic over fake objects and in-process adapters."""

import io
import json
import os
import re
import sys
import types
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_pm.delegate import ConfigError, Outcome, Spec, cli
from agent_pm.delegate.composer import Composer, RunParams
from agent_pm.delegate.composer.base import TEAM
from agent_pm.delegate.composer.prompt import RESUME
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.delegate.engine.runner import STOP_LIMIT
from agent_pm.delegate.engine.state import RunState
from agent_pm.delegate.engine.types import Layout
from agent_pm.session import NotDisplayable, SessionError
from agent_pm.utils import Record, Status

from delegate.tests.case import Case, committed
from delegate.tests.cli_case import (
    HARNESS,
    HEADS,
    KEYS,
    PLACEMENT,
    SID,
    TUI,
    Base,
    RefusingAdapter,
)
from delegate.tests.fakes.harness_session import HarnessSession, RecordingHost
from delegate.tests.harness import SID as HARNESS_SID
from delegate.tests.harness import HarnessCase
from delegate.tests.replies import block
from delegate.tests.unit.runs import DONE, START, STOP, FakeHandle, no_child, turn
from session.tests import fake_adapter
from session.tests.modules import adapter_loader
from utils.tests.case import local_section


class Detaching:
    """A delegate class for `run --detach`: records each construction's role, Spec and placement,
    and its environment; its detach returns RECORD or raises `failure`.
    """

    made: list = []
    environments: list = []
    failure: Exception | None = None

    def __init__(self, role, spec, **placement):
        Detaching.made.append((role, spec, placement))
        Detaching.environments.append(dict(os.environ))
        if isinstance(Detaching.failure, ConfigError):
            raise Detaching.failure

    def detach(self, *, outer=None):
        if Detaching.failure is not None:
            raise Detaching.failure
        return RECORD


RECORD = Record(
    1,
    "role",
    TUI,
    (HARNESS, TUI),
    SID,
    "/work",
    ("agent-pm-delegate",),
    ("agent-pm-delegate", "resume", "/work"),
    "mgr",
    "dummy-tester",
)


class Detach(Base):
    """`run --detach` on a Detaching delegate class; events file `events`."""

    def setUp(self):
        super().setUp()
        self.events = os.path.join(self.tmp, "events")
        Detaching.made, Detaching.environments, Detaching.failure = [], [], None
        self.detaching = True

    def delegate_cls(self):
        return Detaching if self.detaching else super().delegate_cls()

    def detaches(self, *extra, events=None, **kw):
        return self.runs("--sid", SID, "--detach", "--events", events or self.events, *extra, **kw)

    def test_inside_an_agent_session_a_run_detached_or_not_exits_2_and_constructs_nothing(self):
        with mock.patch.dict(os.environ, {"AGENT_PM_AGENT_SESSION": "1"}):
            for code, out, err in (self.detaches(), self.runs("--sid", SID)):
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("delegate: refused inside an agent session"), err)
        self.assertEqual(Detaching.made, [])

    def test_inside_an_agent_session_list_dry_run_and_help_still_run(self):
        self.detaching = False
        with mock.patch.dict(os.environ, {"AGENT_PM_AGENT_SESSION": "1"}):
            self.assertEqual(self.cli("run", "--list")[0], 0)
            code, _, err = self.runs("--sid", SID, "--dry-run")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.cli("--help")[0], 0)

    def test_usage_errors_exit_2_and_construct_nothing(self):
        e = self.events
        for argv, out, want in (
            (("--detach",), None, "--detach needs --events"),
            (("--events", e), None, "--events needs --detach"),
            (("--events", e, "--mode", "tui"), None, "--events needs --detach"),
            ((), "-", "--out takes a PATH, not -"),
            (("--dry-run",), "-", "--out takes a PATH, not -"),
            (("--detach", "--events", e), "-", "--out takes a PATH, not -"),
            (("--detach", "--events", e, "--dry-run"), "-", "--out takes a PATH, not -"),
            (
                ("--detach", "--events", e, "--mode", "in_process"),
                None,
                "--detach needs --mode headless or tui",
            ),
        ):
            with self.subTest(argv=argv, out=out):
                self.assertEqual(self.runs(*argv, out=out), (2, "", f"delegate: {want}\n"))
        self.assertEqual(Detaching.made, [])

    def test_without_out_the_spec_out_is_none(self):
        code, out, err = self.cli(
            "run",
            "--role",
            "dummy-tester",
            "--input",
            "x",
            "--workdir",
            self.work,
            "--sid",
            SID,
            "--detach",
            "--events",
            self.events,
        )
        self.assertEqual(
            (code, out, err), (0, RECORD.to_line() + "\n", f"delegate: session {SID}\n")
        )
        ((_, spec, _),) = Detaching.made
        self.assertIsNone(spec.out)

    def test_cwd_is_the_specs_cwd(self):
        cwd = os.path.join(self.tmp, "elsewhere")
        self.assertEqual(self.detaches("--cwd", cwd)[0], 0)
        self.assertEqual(self.detaches()[0], 0)
        self.assertEqual([spec.cwd for _, spec, _ in Detaching.made], [cwd, None])

    def test_split_options_are_gone(self):
        for extra in (("--split", "right"), ("--split-from", "mgr")):
            with self.subTest(extra=extra):
                code, out, err = self.detaches("--mode", "tui", *extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("unrecognized arguments", err)
        self.assertEqual(Detaching.made, [])

    def test_detach_prints_one_record_line_and_names_its_session(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                Detaching.made = []
                code, out, err = self.detaches("--mode", mode)
                self.assertEqual(
                    (code, out, err), (0, RECORD.to_line() + "\n", f"delegate: session {SID}\n")
                )
                ((role, spec, placement),) = Detaching.made
                self.assertEqual((role, placement), ("dummy-tester", {"display": None}))
                self.assertEqual(
                    (spec.mode, spec.sid, spec.events, spec.workdir, spec.out),
                    (mode, SID, self.events, self.work, self.out),
                )

    def test_detach_events_are_the_given_file_else_the_launchers(self):
        for extra, want in (((), "launcher/events"), (("--events", self.events), self.events)):
            with self.subTest(extra=extra):
                Detaching.made, Detaching.environments = [], []
                os.environ["AGENT_PM_EVENTS"] = "launcher/events"
                code, _, err = self.runs("--sid", SID, "--detach", *extra)
                self.assertEqual(code, 0, err)
                self.assertEqual(Detaching.made[0][1].events, want)
                self.assertNotIn("AGENT_PM_EVENTS", Detaching.environments[0])
                self.assertNotIn("AGENT_PM_EVENTS", os.environ)

    def test_the_launchers_sid_is_the_runs_and_a_different_sid_exits_2(self):
        other = "99999999-8888-7777-6666-555555555555"
        for extra in ((), ("--sid", SID), ("--resume",), ("--sid", other)):
            with self.subTest(extra=extra):
                Detaching.made, Detaching.environments = [], []
                os.environ["AGENT_PM_SID"] = SID
                code, out, err = self.runs("--detach", "--events", self.events, *extra)
                self.assertNotIn("AGENT_PM_SID", os.environ)
                if other in extra:
                    want = f"delegate: $AGENT_PM_SID {SID} differs from --sid {other}\n"
                    self.assertEqual((code, out, err, Detaching.made), (2, "", want, []))
                    continue
                self.assertEqual(code, 0, err)
                ((_, spec, _),) = Detaching.made
                self.assertEqual((spec.sid, spec.resume), (SID, extra == ("--resume",)))
                self.assertNotIn("AGENT_PM_SID", Detaching.environments[0])

    def test_an_unusable_events_file_given_or_inherited_exits_2_writing_nothing(self):
        self.detaching = False
        os.mkdir(self.events)
        misread = os.path.join(self.tmp, "ev#1")
        for given, why in (
            (self.events, f"events file {self.events}: Is a directory"),
            (misread, f"events file {misread}: tmux would misread it"),
        ):
            for extra, launcher in ((("--events", given), {}), ((), {"AGENT_PM_EVENTS": given})):
                with self.subTest(extra=extra, launcher=launcher):
                    os.environ.update(launcher)
                    code, out, err = self.runs("--sid", SID, "--detach", *extra)
                    self.assertEqual((code, out, err), (2, "", f"delegate: {why}\n"))
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_a_display_is_given_to_the_delegate_unread(self):
        code, _, err = self.detaches("--mode", "tui", "--display", "mgr:%1 right")
        self.assertEqual(code, 0, err)
        self.assertEqual(Detaching.made[0][2], {"display": "mgr:%1 right"})

    def test_input_dash_is_read_by_the_caller(self):
        code, _, err = self.detaches(input="-", stdin="-x\nUse B.\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(Detaching.made[0][1].input, "-x\nUse B.\n")

    def test_a_refusal_exits_2_and_a_start_failure_1_without_a_record(self):
        for failure, code in (
            (ConfigError(f"{self.work}: delegate is still alive"), 2),
            (SessionError(f"detach {HARNESS}: duplicate session"), 1),
            (SessionError(f"tui session {TUI} did not start"), 1),
            (OSError("No space left on device"), 1),
        ):
            with self.subTest(failure=failure):
                Detaching.failure = failure
                self.assertEqual(self.detaches(), (code, "", f"delegate: {failure}\n"))

    def test_on_event_is_split_once_into_the_callback_argv_never_a_shell(self):
        code, out, err = self.detaches("--on-event", "writeback --issue 'A B' $X;rm *")
        self.assertEqual((code, out), (0, RECORD.to_line() + "\n"), err)
        ((_, _, placement),) = Detaching.made
        self.assertEqual(
            placement,
            {"display": None, "callback": ("writeback", "--issue", "A B", "$X;rm", "*")},
        )

    def test_an_unusable_on_event_exits_2_and_constructs_nothing(self):
        for extra in (
            ("--on-event", "writeback 'A B"),
            ("--on-event", ""),
            ("--on-event", " \t "),
            ("--on-event", "a", "--on-event", "b"),
        ):
            with self.subTest(extra=extra):
                code, out, err = self.detaches(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--on-event", err)
        self.assertEqual(
            self.runs("--on-event", "writeback"),
            (2, "", "delegate: --on-event needs --detach\n"),
        )
        self.assertEqual(Detaching.made, [])

    def test_a_dry_run_shows_the_callback_argv_and_writes_nothing(self):
        self.detaching = False
        code, out, err = self.detaches("--dry-run", "--on-event", "writeback --issue 'A B'")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["callback"], ["writeback", "--issue", "A B"])
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_dry_run_adds_the_session_names_and_the_events_file(self):
        self.detaching = False
        for mode, tui in (("headless", None), ("tui", TUI)):
            with self.subTest(mode=mode):
                code, out, err = self.detaches("--mode", mode, "--dry-run", events="events")
                self.assertEqual(code, 0, err)
                shown = json.loads(out)
                self.assertEqual(set(shown), KEYS | PLACEMENT | {"events"})
                self.assertEqual(
                    (shown["mode"], shown["sessions"], shown["hosts"], shown["events"]),
                    (
                        mode,
                        {"agent": tui, "harness": HARNESS},
                        {"agent": "tmux", "harness": "tmux"},
                        self.events,
                    ),
                )
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))
        self.assertFalse(os.path.exists(self.events))


class Shown(HarnessSession):
    """Completes a display as `shown:<value>`, recording each (value, host) it is asked."""

    asked: list = []

    @classmethod
    def display(cls, value=None, *, host=None):
        cls.asked.append((value, host))
        return f"shown:{value}"


class Harnessed(HarnessCase):
    """cli.main `run` on HarnessDelegate over Shown sessions on RecordingHosts (default `agent`),
    cwd the test's HOME: no process starts.
    """

    def setUp(self):
        super().setUp()
        Shown.asked = []
        for key in ("AGENT_PM_SID", "AGENT_PM_EVENTS"):
            os.environ.pop(key, None)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.home)
        self.events = os.path.join(self.home, "events")

    def cli(self, *extra, input="Hello.", stdin=None):
        class Bound(HarnessDelegate):
            def __init__(self, role, spec, **placement):
                super().__init__(role, spec, session_cls=Shown, **placement)

        argv = ["run", "--role", "dummy-tester", "--task", "echo", "--input", input]
        argv += ["--out", os.path.join(self.work, "out.md"), "--workdir", self.work]
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch("sys.stdin", stdin or io.StringIO()),
        ):
            try:
                code = cli.main([*argv, "--client", "fake", *extra], delegate_cls=Bound)
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()


class DetachDisplay(Harnessed):
    def test_a_given_or_absent_display_is_completed_once_on_the_agent_host_and_kept(self):
        for given, extra in (("mgr:%1 right", ("--display", "mgr:%1 right")), (None, ())):
            with self.subTest(given=given):
                HarnessSession.reset()
                Shown.asked = []
                self.work = os.path.join(self.home, "given" if given else "absent")
                code, out, err = self.cli(
                    "--sid", HARNESS_SID, "--detach", "--events", self.events, *extra
                )
                self.assertEqual(code, 0, err)
                self.assertEqual(Shown.asked, [(given, "agent")])
                shown = f"shown:{given}"
                self.assertEqual(Record.parse(out).display, shown)
                self.assertEqual(self.metadata()["display"], shown)
                self.assertEqual(RecordingHost.launches[0]["display"], shown)


class DetachEvents(Harnessed):
    def test_a_relative_events_file_given_or_inherited_is_stored_absolute(self):
        want = os.path.join(os.getcwd(), "events")
        for name, extra, launcher in (
            ("given", ("--events", "events"), {}),
            ("inherited", (), {"AGENT_PM_EVENTS": "events"}),
        ):
            with self.subTest(name):
                HarnessSession.reset()
                self.work = os.path.join(self.home, name)
                os.environ.update(launcher)
                code, _, err = self.cli("--sid", HARNESS_SID, "--detach", *extra)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.metadata()["events"], want)
                self.assertEqual(RecordingHost.launches[0]["events"], want)


class ReadOnce(io.StringIO):
    def read(self, size=-1):
        if self.tell():
            raise AssertionError("stdin read twice")
        return super().read(size)


class DetachInput(Harnessed):
    def test_input_dash_is_read_once_by_the_caller_and_stored_and_prompted_verbatim(self):
        with open(os.path.join(self.home, "notes.md"), "w", encoding="utf-8") as file:
            file.write("not the input\n")
        for name, text in (
            ("file", "notes.md"),
            ("lines", "first line\n\nthird line\n"),
            ("dash", "-x --detach\n"),
            ("unicode", "Résumé ✓ 中文\n"),
        ):
            with self.subTest(name):
                HarnessSession.reset()
                self.work = os.path.join(self.home, name)
                code, _, err = self.cli(
                    "--sid",
                    HARNESS_SID,
                    "--detach",
                    "--events",
                    self.events,
                    input="-",
                    stdin=ReadOnce(text),
                )
                self.assertEqual(code, 0, err)
                state = self.metadata()
                self.assertEqual(state["spec"]["input"], text)
                self.assertIn(f"# Input\n\n{text}", state["plan"]["session"]["prompt"])
                harness = "agent_pm.delegate.engine.harness"
                self.assertEqual(
                    RecordingHost.launches[0]["argv"],
                    [sys.executable, "-P", "-s", "-m", harness, self.work],
                )


class DetachSid(Harnessed):
    def test_the_launchers_sid_is_stored_and_printed(self):
        os.environ["AGENT_PM_SID"] = HARNESS_SID
        code, out, err = self.cli("--detach", "--events", self.events)
        self.assertEqual(code, 0, err)
        self.assertEqual(Record.parse(out).sid, HARNESS_SID)
        self.assertEqual(self.metadata()["spec"]["sid"], HARNESS_SID)
        self.assertNotIn("AGENT_PM_SID", RecordingHost.launches[0]["env"])

    def test_a_different_sid_starts_and_writes_nothing(self):
        os.environ["AGENT_PM_SID"] = HARNESS_SID
        code, out, _ = self.cli("--sid", SID, "--detach", "--events", self.events)
        self.assertEqual((code, out), (2, ""))
        self.assertEqual((RecordingHost.launches, os.path.exists(self.work)), ([], False))


class DryRun(Base):
    def test_dry_run_prints_the_plan_and_creates_nothing(self):
        code, out, err = self.runs("--sid", SID, "--dry-run")
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(set(shown), KEYS | PLACEMENT)
        self.assertEqual(
            {k: v for k, v in shown.items() if k not in ("dirs", "reads", "commands", "prompt")},
            {
                "client": "fake",
                "mode": "headless",
                "sid": SID,
                "resume": False,
                "cwd": Layout(self.work).agent,
                "project": False,
                "tier": 3,
                "effort": "low",
                "sessions": {"agent": None, "harness": None},
                "hosts": {"agent": "tmux", "harness": "tmux"},
                "callback": None,
            },
        )
        self.assertEqual(
            (shown["dirs"], shown["reads"], shown["commands"]),
            ([], [os.path.join(TEAM, "tasks")], []),
        )
        self.assertIn("**Your task**: `echo` (`<tasks>/echo.md`).", shown["prompt"])
        self.assertTrue(shown["prompt"].endswith("\n# Input\n\nHello.\n"))
        self.assertEqual(err, "")
        self.assertEqual(
            (self.launches(), os.path.exists(self.work), os.listdir(self.agent_pm)), ([], False, [])
        )

    def test_a_dry_run_configures_the_session(self):
        code, stdout, err = self.runs("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(stdout)["client"], "fake")

    def test_a_new_run_gets_a_sid_and_a_resume_continues_its_task(self):
        code, out, _ = self.runs("--dry-run", task=None)
        shown = json.loads(out)
        self.assertEqual((code, uuid.UUID(shown["sid"]).version, shown["resume"]), (0, 4, False))
        self.assertIn("**Your task**: pick it from", shown["prompt"])
        code, out, _ = self.runs("--sid", SID, "--resume", "--dry-run")
        shown = json.loads(out)
        self.assertEqual(
            (code, shown["sid"], shown["resume"], shown["cwd"]),
            (0, SID, True, Layout(self.work).agent),
        )
        self.assertTrue(shown["prompt"].startswith(RESUME))
        self.assertFalse(os.path.exists(self.work))


class InProcess(Base):
    """
    The in_process-only fake, and RefusingAdapter (client `refusing`); no child process starts.
    """

    def setUp(self):
        super().setUp()
        refusing = types.ModuleType("agent_pm.session.adapters.refusing")
        refusing.RefusingAdapter = RefusingAdapter
        self.session_dependencies = {
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

    def test_a_run_without_sid_takes_the_launchers(self):
        os.environ["AGENT_PM_SID"] = SID
        code, _, err = self.runs("--mode", "in_process")
        self.assertEqual(code, 0, err)
        self.assertIn(f"delegate: session {SID}\n", err)

    def test_the_launchers_events_are_ignored_without_detach(self):
        os.environ["AGENT_PM_EVENTS"] = os.path.join(self.tmp, "events")
        code, _, err = self.runs("--mode", "in_process", "--sid", SID)
        self.assertEqual(code, 0, err)
        self.assertNotIn("AGENT_PM_EVENTS", os.environ)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "events")))

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

    def test_a_run_without_out_names_its_default_deliverable_file(self):
        with no_child():
            code, out, err = self.cli(
                "run",
                "--role",
                "dummy-tester",
                "--task",
                "echo",
                "--input",
                "Hello.",
                "--workdir",
                self.work,
                "--client",
                "fake",
                "--mode",
                "in_process",
                "--sid",
                SID,
            )
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn(f"delegate: deliverable {Layout(self.work).out}\n", err)

    def test_a_run_reports_its_deliverable_and_leaves_its_delegate_json(self):
        code, out, err = self.runs("--mode", "in_process", "--sid", SID)
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn(f"delegate: deliverable {self.out}\n", err)
        with open(RunState(Layout(self.work)).record) as f:
            state = json.load(f)
        self.assertEqual(
            (state["role"], state["attempt"], state["completed"]), ("dummy-tester", 1, True)
        )
        self.assertEqual(
            (sorted(os.listdir(self.work)), sorted(os.listdir(Layout(self.work).run))),
            (
                ["agent", "out.md", "run"],
                ["delegate.json", "delegate.lock", "delegate.run.lock", "run.jsonl"],
            ),
        )

    def test_an_answer_while_the_run_is_alive_exits_2_and_starts_nothing(self):
        spec = Spec("Hello.", self.work, task="echo", mode="in_process", client="fake", sid=SID)
        self.delegate_cls()("dummy-tester", spec)
        with RunState(Layout(self.work)).run_lock():
            code, out, err = self.runs("--mode", "in_process", "--sid", SID, "--resume")
        self.assertEqual(
            (code, out, err), (2, "", f"delegate: {self.work}: delegate is still alive\n")
        )
        self.assertEqual(self.launches(), [])

    def test_headless_and_tui_exit_2_and_nothing_runs(self):
        for mode, want in (
            ("headless", "client 'fake' does not support headless mode"),
            ("tui", "client 'fake' does not support tui mode"),
        ):
            with self.subTest(mode=mode):
                code, out, err = self.runs("--mode", mode)
                self.assertEqual((code, out, err.splitlines()[-1]), (2, "", f"delegate: {want}"))
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_claude_and_codex_refuse_it_with_exit_2(self):
        self.configure_session(committed())
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                code, out, err = self.runs("--mode", "in_process", client=client)
                self.assertEqual(
                    (code, out, err.splitlines()[-1]),
                    (2, "", f"delegate: client {client!r} does not support in_process mode"),
                )
                self.assertFalse(os.path.exists(self.work))

    def test_dry_run_shows_the_mode(self):
        code, out, err = self.runs("--mode", "in_process", "--dry-run")
        self.assertEqual((code, json.loads(out)["mode"]), (0, "in_process"), err)
        self.assertFalse(os.path.exists(self.work))

    def test_a_check_refusal_exits_2_with_no_record(self):
        code, out, err = self.runs("--mode", "in_process", client="refusing")
        self.assertEqual((code, out, err.splitlines()[-1]), (2, "", "delegate: refused by check"))
        self.assertEqual(
            (os.path.exists(os.path.join(Layout(self.work).run, "run.jsonl")), self.launches()),
            (False, []),
        )


class Tui(Base):
    def setUp(self):
        super().setUp()
        self.configure_fake(tui=True)

    def test_tui_mode_runs_the_tui_policy_in_its_named_session_a_give_up_exiting_1(self):
        for events, want, last in (
            ([turn(START, block(DONE))], 0, "delegate: status done"),
            ([STOP] * (1 + STOP_LIMIT), 1, "delegate: the agent run returned no outcome"),
        ):
            with self.subTest(want=want):
                handle = FakeHandle(events)
                with (
                    mock.patch.object(self.driver, "preflight"),
                    mock.patch.object(self.driver, "start", side_effect=handle.start) as start,
                ):
                    code, out, err = self.runs("--mode", "tui")
                self.assertEqual((code, out, err.splitlines()[-1]), (want, "", last))
                spec = start.call_args.args[0].session
                self.assertEqual(
                    ((spec.client, spec.mode), spec.name, spec.events),
                    (("fake", "tui"), f"dummy-tester-{spec.sid[:8]}", None),
                )

    def test_a_client_without_tui_exits_2_and_a_dry_run_plans_it(self):
        self.configure_fake()
        code, out, err = self.runs("--mode", "tui")
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err.splitlines()[-1], "delegate: client 'fake' does not support tui mode")
        self.assertFalse(os.path.exists(self.work))
        code, out, _ = self.runs("--mode", "tui", "--dry-run")
        self.assertEqual((code, json.loads(out)["mode"], self.launches()), (0, "tui", []))


class List(Base):
    def test_list_prints_each_role_and_task_with_its_source(self):
        code, out, err = self.cli("run", "--list")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual([line for line in lines if not line.startswith("  ")], HEADS)
        echo = Composer().index("dummy-tester")["echo"]
        self.assertTrue(
            out.endswith(f"\ndummy-tester  local  built-in\n  echo: {echo}  [built-in]\n"), out
        )
        self.assertTrue(
            all(line.endswith("  [built-in]") for line in lines if line.startswith("  "))
        )
        self.assertNotIn("<tasks>", out)
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_list_abbreviated_is_list_and_takes_no_other_option(self):
        self.assertEqual(self.cli("run", "--li"), self.cli("run", "--list"))
        for argv, extra in (
            (["--list", "--role", "pm"], "--role"),
            (["--li", "--rol", "pm"], "--rol"),
            (["--list", "--dry-run"], "--dry-run"),
            (["--list", "--client=fake"], "--client=fake"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.cli("run", *argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"--list takes no other option, not {extra}\n", err)
        code, out, err = self.cli("run")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("the following arguments are required: --role", err)

    def test_list_stops_at_a_config_defect(self):
        for local, want in (
            (
                "[roles.ghost]\ntier = 3\n",
                "roles.ghost: no charter roles/ghost.md in core or team_dirs",
            ),
            ('team_dirs = "x"\n', "team_dirs: want a list of absolute or ~ paths, got 'x'"),
            (
                "bogus = 1\n",
                "config: ~/.agent-pm/config.local.toml: unknown key 'bogus' in [delegate]",
            ),
            ('tier = "2"\n', "tier must be an integer 1–4, got '2'"),
            (
                'effort = "ultra"\n',
                "effort must be one of low, medium, high, xhigh, max, got 'ultra'",
            ),
            ('read = "~/notes"\n', "read must be a list of strings, got '~/notes'"),
            ("write = 3\n", "write must be a list of strings, got 3"),
            ('[roles.pm]\ncommands = "ls *"\n', "commands must be a list of strings, got 'ls *'"),
            ("[roles.pm]\ntemplates = [3]\n", "templates must be a list of strings, got [3]"),
        ):
            with self.subTest(local=local):
                self.local(local)
                self.assertEqual(self.cli("run", "--list"), (2, "", f"delegate: {want}\n"))


class Configures(Base):
    def test_a_session_config_error_exits_2_and_starts_nothing(self):
        self.configure_session({})
        code, out, err = self.runs()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("[adapters.fake]", err)
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_an_unknown_harness_host_or_delegates_key_exits_2_and_starts_nothing(self):
        for text, want in (
            (
                '[delegates]\nharness_host = "nope"\n',
                "delegate: [delegates] harness_host: want one of pty, tmux",
            ),
            (
                "[delegates]\nkind = 1\n",
                "delegate: [delegates] kind: the delegate takes only harness_host",
            ),
        ):
            with self.subTest(text):
                self.local(text)
                code, out, err = self.runs()
                self.assertEqual((code, out, err), (2, "", f"{want}\n"))
        self.assertEqual((self.launches(), os.path.exists(self.work)), ([], False))

    def test_list_works_without_a_client(self):
        self.configure_session({})
        code, _, err = self.cli("run", "--list")
        self.assertEqual((code, err), (0, ""))


class Usage(Base):
    def test_a_run_needs_input_and_workdir(self):
        for argv in (
            ("--out", "o.md"),
            ("--workdir", self.work),
            ("--input", "x", "--out", "o.md"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(
                    self.cli("run", "--role", "dummy-tester", "--client", "fake", *argv),
                    (2, "", "delegate: --input and --workdir are required\n"),
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

    def test_client_given_twice_exits_2(self):
        for extra in (("--client", "fake"), ("--client=fake",)):
            with self.subTest(extra=extra):
                code, out, err = self.runs(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--client given twice", err)
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
            ("an unknown client", {"client": "nope"}, "", "no [adapters.nope] table"),
        ]
        for name, kw, local, want in cases:
            self.local(local)
            for dry in ((), ("--dry-run",)) if name != "an unknown client" else ((),):
                with self.subTest(name, dry=dry):
                    code, out, err = self.runs(*dry, **kw)
                    self.assertEqual((code, out), (2, ""), err)
                    self.assertTrue(err.splitlines()[-1].startswith(f"delegate: {want}"), err)
                    self.assertFalse(os.path.exists(self.work))
        self.assertEqual(self.launches(), [])


class Prompt(Case):
    def test_write_failures_are_clean_cli_errors(self):
        os.makedirs(self.out)
        code, out, err = self.prompt()
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("delegate: "), err)
        self.assertIn(self.out, err)
        self.assertNotIn("Traceback", err)

    def test_an_unwritable_workdir_is_a_clean_cli_error(self):
        with mock.patch("pathlib.Path.mkdir", side_effect=PermissionError("write denied")):
            code, out, err = self.prompt()
        self.assertEqual((code, out, err), (2, "", "delegate: write denied\n"))

    def setUp(self):
        self.work = os.path.join(self.home, "work")
        self.out = os.path.join(self.work, "prompt.md")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.home)

    def prompt(self, *extra, role="dummy-tester", input="Hello.\n"):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(
                    [
                        "prompt",
                        "--role",
                        role,
                        "--task",
                        "echo",
                        "--input",
                        input,
                        "--workdir",
                        self.work,
                        "--out",
                        self.out,
                        *extra,
                    ]
                )
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def test_instruction_matches_composer_bytes_no_client_or_session_configuration(self):
        Path(self.agent_pm, "config.local.toml").write_text(
            local_section('[hosts]\ndefault = "missing"\n', "session"), encoding="utf-8"
        )
        composition = Composer()
        config = composition.resolve("dummy-tester", "echo")
        expected = composition.render(
            config, RunParams(input="Hello.\n", out=self.out, workdir=Layout(self.work).agent)
        )
        with mock.patch("subprocess.Popen", side_effect=AssertionError("session started")):
            self.assertEqual(
                self.prompt(), (0, "", f"delegate: instruction delivered: {self.out}\n")
            )
        self.assertEqual(Path(self.out).read_bytes(), expected.encode("utf-8"))
        log = Path(Layout(self.work).run, "run.jsonl")
        self.assertFalse(log.exists())

    def test_runtime_execution_options_are_usage_errors_before_writes(self):
        for argv in (
            ("--client", "claude"),
            ("--mode", "tui"),
            ("--detach",),
            ("--sid", "11111111-2222-3333-4444-555555555555"),
            ("--resume",),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.prompt(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("unrecognized arguments:", err)
                self.assertFalse(os.path.exists(self.work))

    def test_list_is_rejected_and_absent_from_prompt_help(self):
        code, out, err = self.prompt("--list")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unrecognized arguments: --list", err)
        code, out, err = self.prompt("--help")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("--list", out)
        self.assertFalse(os.path.exists(self.work))

    def test_out_dash_is_refused(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli.main(
                ["prompt", "--role", "dummy-tester", "--input", "x"]
                + ["--workdir", self.work, "--out", "-"]
            )
        self.assertEqual((code, err.getvalue()), (2, "delegate: --out takes a PATH, not -\n"))
        self.assertFalse(os.path.exists(self.work))

    def test_hidden_driver_option_is_removed(self):
        code, out, err = self.prompt("--driver", "obsolete")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unrecognized arguments:", err)
        self.assertFalse(os.path.exists(self.work))

    def test_unknown_role_is_a_config_error_before_writes(self):
        code, out, err = self.prompt(role="ghost")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("unknown role 'ghost'", err)
        self.assertFalse(os.path.exists(self.work))


class Commands(Base):
    def test_help_lists_exactly_the_eight_subcommands(self):
        code, out, err = self.cli("--help")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            re.search(r"\{([a-z,-]+)\}", out)[1].split(","),
            ["run", "status", "outcome", "resume", "show", "close", "attach-command", "prompt"],
        )

    def test_an_unknown_missing_or_flat_command_exits_2(self):
        for argv, want in (
            (("bogus",), "invalid choice: 'bogus'"),
            ((), "the following arguments are required: command"),
            (("--role", "dummy-tester", "--list"), "invalid choice: 'dummy-tester'"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(want, " ".join(err.split()))


class View:
    """A run's scripted view: its Status name, its outcome, attach command and errors."""

    def __init__(self, name, *, outcome=None, command=None, error=None, show_error=None):
        self.name, self.outcome_value, self.command = name, outcome, command
        self.error, self.show_error, self.closed = error, show_error, 0

    def status(self):
        if self.error is not None:
            raise self.error
        return Status(self.name, alive=False, state="absent", outcome=None, attach=self.command)

    def show(self):
        if self.show_error is not None:
            raise self.show_error
        return self.command

    def close(self):
        self.closed += 1

    def outcome(self):
        return self.outcome_value

    def attach_command(self):
        return self.command

    def resume(self, *, mode=None):
        return record(self.name)


def record(name):
    work = f"/w/{name}"
    return Record(
        1,
        "role",
        name,
        (),
        None,
        work,
        ("agent-pm-delegate",),
        ("agent-pm-delegate", "resume", work),
        None,
        None,
    )


class Views(HarnessDelegate):
    """attach(key): the View registered under a workdir key or a Record's name."""

    views: dict = {}

    @classmethod
    def attach(cls, record_or_workdir, **_):
        name = record_or_workdir if isinstance(record_or_workdir, str) else record_or_workdir.name
        view = cls.views[name]
        if isinstance(view, Exception):
            raise view
        return view


class Queries(Case):
    def query(self, *argv, stdin="", **views):
        Views.views = views
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch("sys.stdin", io.StringIO(stdin)),
        ):
            try:
                code = cli.main(list(argv), delegate_cls=Views)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_status_prints_one_line_per_key_in_order_from_arguments_or_stdin(self):
        a, b = View("a"), View("b", command="tmux attach -t =b")
        lines = [
            Status("a", alive=False, state="absent", outcome=None, attach=None).to_line(),
            Status(
                "b", alive=False, state="absent", outcome=None, attach="tmux attach -t =b"
            ).to_line(),
        ]
        self.assertEqual(
            self.query("status", "a", "b", a=a, b=b), (0, f"{lines[0]}\n{lines[1]}\n", "")
        )
        stdin = f"{record('b').to_line()}\n{record('a').to_line()}\n"
        self.assertEqual(
            self.query("status", stdin=stdin, a=a, b=b), (0, f"{lines[1]}\n{lines[0]}\n", "")
        )

    def test_a_failing_key_is_named_and_the_others_answered(self):
        for error, code in (
            (ConfigError("/w/gone: no delegate.json"), 2),
            (SessionError("tmux: boom"), 1),
        ):
            with self.subTest(error=error):
                got = self.query("status", "gone", "a", gone=error, a=View("a"))
                self.assertEqual(
                    got,
                    (
                        code,
                        Status(
                            "a", alive=False, state="absent", outcome=None, attach=None
                        ).to_line()
                        + "\n",
                        f"delegate: gone: {error}\n",
                    ),
                )

    def test_two_keys_with_one_name_answer_nothing(self):
        code, out, err = self.query("close", "x", "y", x=View("same"), y=View("same"))
        self.assertEqual((code, out, err), (2, "", "delegate: name same given more than once\n"))
        self.assertEqual((Views.views["x"].closed, Views.views["y"].closed), (0, 0))

    def test_show_prints_each_command_and_exits_1_without_a_displayable_session(self):
        shown = View("a", command="tmux attach -t =a")
        none = View("b", show_error=NotDisplayable("delegate has no live displayable session"))
        self.assertEqual(
            self.query("show", "a", "b", a=shown, b=none),
            (1, "tmux attach -t =a\n", "delegate: b: delegate has no live displayable session\n"),
        )

    def test_close_closes_each_key_and_prints_nothing(self):
        a, b = View("a"), View("b")
        self.assertEqual(self.query("close", "a", "b", a=a, b=b), (0, "", ""))
        self.assertEqual((a.closed, b.closed), (1, 1))

    def test_outcome_prints_one_json_object_or_null(self):
        done = Outcome("done", "T", "S", deliverable="# Doc\n")
        self.assertEqual(
            self.query("outcome", "a", a=View("a", outcome=done)),
            (
                0,
                '{"status":"done","title":"T","summary":"S","questions":[],"url":"","files":[],"deliverable":"# Doc\\n"}\n',
                "",
            ),
        )
        self.assertEqual(self.query("outcome", "a", a=View("a")), (0, "null\n", ""))
        self.assertEqual(
            self.query("outcome", stdin=record("a").to_line() + "\n", a=View("a")),
            (0, "null\n", ""),
        )

    def test_one_key_commands_refuse_any_other_count(self):
        for command in ("outcome", "attach-command", "resume"):
            with self.subTest(command=command):
                self.assertEqual(
                    self.query(command), (2, "", "delegate: takes exactly one key, got 0\n")
                )
                stdin = record("a").to_line() + "\n" + record("b").to_line() + "\n"
                self.assertEqual(
                    self.query(command, stdin=stdin),
                    (2, "", "delegate: takes exactly one key, got 2\n"),
                )
                code, out, err = self.query(command, "a", "b", a=View("a"), b=View("b"))
                self.assertEqual((code, out), (2, ""))
                self.assertIn("unrecognized arguments: b", err)

    def test_attach_command_prints_one_command_or_exits_1(self):
        self.assertEqual(
            self.query("attach-command", "a", a=View("a", command="tmux attach -t =a")),
            (0, "tmux attach -t =a\n", ""),
        )
        self.assertEqual(
            self.query("attach-command", "a", a=View("a")),
            (1, "", "delegate: a: no displayable session\n"),
        )

    def test_resume_inside_an_agent_session_exits_2_and_status_still_answers(self):
        a = View("a")
        with mock.patch.dict(os.environ, {"AGENT_PM_AGENT_SESSION": "1"}):
            code, out, err = self.query("resume", "a", a=a)
            self.assertEqual((code, out), (2, ""))
            self.assertTrue(err.startswith("delegate: refused inside an agent session"), err)
            self.assertEqual(self.query("status", "a", a=a)[0], 0)

    def test_resume_prints_the_record_line(self):
        self.assertEqual(
            self.query("resume", "a", a=View("a")), (0, record("a").to_line() + "\n", "")
        )
