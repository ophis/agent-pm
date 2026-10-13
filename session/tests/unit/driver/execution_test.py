"""Driver execution behavior with caller-owned dependencies."""

import contextlib
import inspect
import itertools
import json
import os
import threading
from unittest import mock

from agent_pm.session.engine import api
from agent_pm.session.engine.types import (
    Access,
    ConfigError,
    DriverError,
    Exited,
    IdleTimeout,
    Reply,
    SessionBound,
    SessionInfo,
    Stderr,
    TurnEnd,
    Usage,
    Window,
)
from agent_pm.session.modes.headless import types as headless
from agent_pm.session.modes.in_process import types as in_process

from session.tests.case import SPAWNS, Case
from session.tests.unit.driver.support import (
    LAUNCHED,
    SID,
    Base,
)


class HandleTest(Base):
    def test_send_and_screen_are_tui_only(self):
        handle = self.start()
        for call, message in (
            (lambda: handle.send("x"), "send: tui mode only"),
            (handle.screen, "screen: tui mode only"),
        ):
            with self.subTest(message=message), self.assertRaises(DriverError) as caught:
                call()
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.drain(handle), [Exited(0)])

    def test_state_is_live_while_the_process_runs_then_absent(self):
        self.steps({"sleep": 1})
        handle = self.start()
        self.assertEqual(handle.state(), "live")
        self.assertEqual(self.drain(handle), [Exited(0)])
        self.assertEqual(handle.state(), "absent")

    def test_attributes_info_reply_and_usage(self):
        cwd = os.path.join(self.dir, "cwd")
        os.mkdir(cwd)
        handle = self.start(spec=self.spec(access=Access(cwd=cwd, project=True)), sid="s1")
        self.assertEqual((handle.sid, handle.runtime, handle.mode), ("s1", "fake", "headless"))
        info = SessionInfo(
            "s1", "", cwd, True, os.path.join(self.workdir, "fake-s1.jsonl"), "fake --resume s1"
        )
        self.assertEqual(handle.info(), info)
        self.assertIsNone(handle.reply)
        self.assertIsNone(handle.usage())
        self.assertEqual(self.drain(handle), [Exited(0)])
        (session,) = self.written()
        self.assertEqual(
            {k: v for k, v in session.items() if k != "ts"},
            {
                "kind": "session",
                "sid": "s1",
                "cwd": cwd,
                "project": True,
                "transcript": info.transcript,
                "resume": info.resume,
            },
        )

    def test_usage_is_its_runtimes(self):
        self.configure("probe", probe={"argv": ("probe-cmd",)})
        handle = self.start(runtime="probe")
        with mock.patch.object(self.driver, "probe", return_value="7") as probe:
            self.assertEqual(handle.usage(), Usage(Window(7.0, None), None))
        probe.assert_called_once()
        self.assertEqual(self.drain(handle), [Exited(0)])


class InProcessTest(Base):
    def setUp(self):
        super().setUp()
        self.configure(implementation="adapters.fake:InProcessFakeAdapter")

    def run_in_process(self, **kwargs) -> tuple[api.Handle, list]:
        """An in_process session started and drained, each of SPAWNS failing meanwhile and never
        called.
        """
        with contextlib.ExitStack() as stack:
            spawns = [
                stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
                for target in SPAWNS
            ]
            handle = self.start(mode="in_process", **kwargs)
            found = self.drain(handle)
        for spawn in spawns:
            spawn.assert_not_called()
        return handle, found

    def logged(self) -> list[dict]:
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_flags_and_env_reach_the_in_process_adapter(self):
        LAUNCHED.clear()
        spec = self.spec(flags=("--custom", "v"), env={"CUSTOM": "value"})
        _, found = self.run_in_process(runtime="checked", spec=spec)
        self.assertEqual(found, [Exited(0)])
        self.assertEqual((LAUNCHED[0].flags, dict(LAUNCHED[0].env)), (spec.flags, dict(spec.env)))

    def test_records_the_session_its_cwd_access_cwd_else_the_workdir_then_runs_it(self):
        cwd = os.path.join(self.dir, "cwd")
        os.mkdir(cwd)
        for given, project in (("", False), (cwd, True)):
            with self.subTest(cwd=given):
                self.workdir = os.path.join(self.dir, f"work-{project}")
                handle, found = self.run_in_process(
                    spec=self.spec(access=Access(cwd=given, project=project)), sid=SID
                )
                self.assertEqual(found, [Exited(0)])
                self.assertEqual(
                    (handle.sid, handle.runtime, handle.mode, handle.name),
                    (SID, "fake", "in_process", None),
                )
                info = SessionInfo(SID, "", given or self.workdir, project, "", "")
                self.assertEqual(handle.info(), info)
                (session,) = self.written()
                self.assertEqual(
                    {k: v for k, v in session.items() if k != "ts"},
                    {
                        "kind": "session",
                        "sid": SID,
                        "cwd": info.cwd,
                        "project": project,
                        "transcript": "",
                        "resume": "",
                    },
                )

    def test_runs_in_this_process_on_another_thread_with_no_child_process(self):
        self.steps({"out": {"type": "message", "text": "hi"}})
        _, found = self.run_in_process()
        self.assertEqual(found, [Reply("hi"), Exited(0)])
        (entry,) = self.logged()
        self.assertEqual(entry["pid"], os.getpid())
        self.assertNotEqual(entry["thread"], threading.get_ident())

    def test_its_events_then_exited_recording_client_session_and_stderr_a_yielded_exited_dropped(
        self,
    ):
        self.steps(
            {"out": {"type": "session", "id": "n1"}},
            {"out": {"type": "message", "text": "hi"}},
            {"err": "warn"},
            {"out": {"type": "exit"}},
            {"out": {"type": "result", "text": "done"}},
        )
        handle, found = self.run_in_process()
        self.assertEqual(
            found,
            [
                SessionBound("n1"),
                Reply("hi"),
                Stderr("warn"),
                TurnEnd("done", 0),
                Exited(0),
            ],
        )
        self.assertEqual(
            [(e["kind"], e.get("client_id"), e.get("text")) for e in self.written()],
            [("session", None, None), ("client_session", "n1", None), ("stderr", None, "warn")],
        )
        self.assertEqual((handle.info().native_id, handle.reply), ("n1", "done"))

    def test_the_runtime_note_follows_the_prompt_only_with_commands(self):
        for commands, prompt in (((), "hello"), (("git",), "hello\n\nFAKE NOTE")):
            with self.subTest(commands=commands):
                self.run_in_process(spec=self.spec(access=Access(commands=commands)))
                self.assertEqual(self.logged()[-1]["prompt"], prompt)

    def test_resume_continues_that_session(self):
        handle, _ = self.run_in_process(resume=SID)
        (entry,) = self.logged()
        self.assertEqual((handle.sid, entry["sid"], entry["resume"]), (SID, SID, True))
        self.assertEqual(self.written()[0]["sid"], SID)

    def test_check_gets_the_same_request_before_record_and_execution(self):
        for kwargs, resume in (({"sid": SID}, False), ({"resume": SID}, True)):
            with self.subTest(kwargs=kwargs):
                self.workdir = os.path.join(self.dir, f"work-{resume}")
                self.run_in_process(
                    runtime="checked", spec=self.spec(access=Access(commands=("git",))), **kwargs
                )
                check, run = self.logged()[-2:]
                self.assertEqual(
                    check,
                    {
                        "check": True,
                        "sid": SID,
                        "resume": resume,
                        "recorded": False,
                    },
                )
                self.assertEqual(
                    (run["prompt"], run["sid"], run["resume"]), ("hello\n\nFAKE NOTE", SID, resume)
                )

    def test_a_config_error_from_check_refuses_with_no_record_and_no_run(self):
        with mock.patch(
            "threading.Thread.start", side_effect=AssertionError("run started")
        ) as thread:
            self.refused(
                ConfigError,
                "tier 4 is unmapped",
                lambda: self.start(runtime="checked", mode="in_process", spec=self.spec(tier=4)),
            )
        thread.assert_not_called()

    def test_takes_no_name_or_events(self):
        self.refused(
            ConfigError, "name: tui mode only", lambda: self.start(mode="in_process", name="w1")
        )
        self.refused(
            ConfigError,
            "events: tui mode only",
            lambda: self.start(mode="in_process", events=self.scenario),
        )

    def test_the_in_process_fake_supports_no_other_mode(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                self.refused(
                    ConfigError,
                    f"runtime 'fake' does not support {mode} mode",
                    lambda *, mode=mode: self.start(mode=mode),
                )

    def test_claude_and_codex_do_not_support_it(self):
        self.configure_session(
            {
                "claude": {"implementation": "adapters.claude:ClaudeAdapter"},
                "codex": {"implementation": "adapters.codex:CodexAdapter"},
            }
        )
        for runtime in ("claude", "codex"):
            with self.subTest(runtime=runtime):
                self.refused(
                    ConfigError,
                    f"runtime {runtime!r} does not support in_process mode",
                    lambda *, runtime=runtime: self.start(runtime=runtime, mode="in_process"),
                )

    def test_a_run_idle_past_idle_timeout_yields_idle_timeout_and_goes_on(self):
        self.steps(
            {"out": {"type": "message", "text": "a"}},
            {"sleep": 0.5},
            {"out": {"type": "message", "text": "b"}},
        )
        _, found = self.run_in_process(idle_timeout=0.1)
        self.assertEqual(found[0], Reply("a"))
        self.assertEqual(set(found[1:-2]), {IdleTimeout(0.1)})
        self.assertEqual(found[-2:], [Reply("b"), Exited(0)])

    def test_a_failed_run_start_is_a_driver_error_after_the_session_record(self):
        with (
            mock.patch(
                "threading.Thread.start", side_effect=RuntimeError("can't start new thread")
            ),
            self.assertRaises(DriverError) as caught,
        ):
            self.start(mode="in_process")
        self.assertEqual(
            str(caught.exception), "cannot start the in_process run: can't start new thread"
        )
        self.assertEqual([e["kind"] for e in self.written()], ["session"])
        self.assertFalse(os.path.exists(self.log))


class ModeTest(Case):
    def test_the_protocols_methods_and_the_headless_and_in_process_modes_have_each_with_its_parameters(
        self,
    ):
        def params(function):
            return [
                (None if p.kind is inspect.Parameter.POSITIONAL_ONLY else p.name, p.kind, p.default)
                for p in inspect.signature(function).parameters.values()
            ]

        members = sorted(
            n for n, v in vars(api.Mode).items() if callable(v) and not n.startswith("_")
        )
        self.assertEqual(members, ["poll", "returncode", "screen", "send", "state", "stop"])
        for mode, name in itertools.product(
            (headless.HeadlessMode, in_process.InProcessMode), members
        ):
            with self.subTest(mode=mode.__name__, name=name):
                self.assertEqual(params(getattr(mode, name)), params(getattr(api.Mode, name)))
