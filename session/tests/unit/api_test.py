"""session/engine/api.py: start()'s checks, its session record, sid and Runtime note, a headless
Handle's surface, usage(), session_status() and writable_dirs(), the session env (Access.env)
reaching a session but not its usage probe, over the fake adapter and test adapters registered
in-process (sys.modules) with [adapters.<name>] tables given to Driver construction. Wherever
nothing may start, subprocess.Popen is patched to fail; usage()'s probe is injected, but for a spawn
error. An in_process start over the in_process-only fake, SPAWNS patched to fail during its run;
claude and codex refuse it. The Mode protocol, and a Handle over a scripted Mode on a fake clock
(time.monotonic patched). The tui paths (start, attach, sessions, close, show, layout,
attach_command, detach) over tmux_host autospecced (no tmux runs).
"""

import contextlib
import dataclasses
import fcntl
import inspect
import itertools
import json
import os
import signal
import stat
import sys
import tempfile
import threading
import types
import unittest
import uuid
from unittest import mock

import agent_pm.session as session_pkg
from agent_pm.session import StartOptions
from agent_pm.session.adapters import selection
from agent_pm.session.engine import api, records
from agent_pm.session.engine import types as engine_types
from agent_pm.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ConfigError,
    DriverError,
    Exited,
    IdleTimeout,
    Launch,
    Reply,
    SessionBound,
    SessionInfo,
    SessionRequest,
    SessionRow,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
    Window,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes import headless, in_process, tui

from session.tests import fake_adapter
from session.tests.case import SPAWNS, Case
from session.tests.modules import module_slot

WAIT = 10  # upper bound on any wait, in seconds
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
PYTHONPATH = "/child"
TMUX = "/opt/tmux/bin/tmux"
BAD_NAMES = ("../x", "a b", "", "a/b", "w1\n", 5)


class BlankAdapter(fake_adapter.FakeAdapter):
    def headless(self):
        return BlankRun(self)


class BlankRun(fake_adapter.FakeHeadless):
    def launch(self, prompt, request: SessionRequest):
        return Launch(())


class FailingAdapter(fake_adapter.FakeAdapter):
    def headless(self):
        return FailingRun(self)


class FailingRun(fake_adapter.FakeHeadless):
    def launch(self, prompt, request: SessionRequest):
        raise LookupError("no binding")


class QuietAdapter(fake_adapter.FakeAdapter):
    runtime_note = ""


class CheckedAdapter(fake_adapter.InProcessFakeAdapter):
    def in_process(self):
        return CheckedRun(self)


class CheckedRun(fake_adapter.FakeInProcess):
    """check: refuses tier 4, records preflight, and retains the request for execution."""

    def check(self, request: SessionRequest):
        self.checked_request = request
        spec = request.spec
        resume = request.resumed
        LAUNCHED.append(spec)
        if spec.tier == 4:
            raise engine_types.ConfigError("tier 4 is unmapped")
        with open(self.adapter.config["log"], "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "check": True,
                        "sid": spec.sid,
                        "resume": resume,
                        "recorded": os.path.exists(records.path(spec.workdir)),
                    }
                )
                + "\n"
            )

    def run(self, prompt, request: SessionRequest):
        assert request is self.checked_request
        yield from super().run(prompt, request)


class ProbeAdapter(fake_adapter.FakeAdapter):
    """The fake adapter with a usage probe, its config's `probe` (Launch's fields), whose stdout is
    the 5-hour window's percent (anything else raises), and a session status naming its sid and cwd.
    """

    keys = fake_adapter.FakeAdapter.keys | {"probe"}

    def usage_probe(self):
        return Launch(**self.config["probe"])

    def usage(self, output):
        return engine_types.Usage(engine_types.Window(float(output), None), None)

    def session_status(self, sid, cwd):
        return engine_types.SessionStatus("yes", 1.5, f"probe {sid} {cwd}")


LAUNCHED: list[Spec] = []


class InnerAdapter(fake_adapter.FakeAdapter):
    """A tui runtime whose sessions set INNER_SESSION in their children; its launch records its spec
    in LAUNCHED, unsets LAUNCH_DROP and sets spec.env, then LAUNCH_SET.
    """

    session_env = "INNER_SESSION"
    identity_env = ("INNER_SESSION", "INNER_ID")
    child_defaults = {"PAGER": "cat", "NO_COLOR": "1"}

    def tui(self):
        return InnerTui(self)


class InnerTui(fake_adapter.FakeTui):
    def launch(self, prompt, request: SessionRequest, hook):
        spec = request.spec
        LAUNCHED.append(spec)
        launch = super().launch(prompt, request, hook)
        env = {**spec.env, "LAUNCH_SET": "1"}
        return dataclasses.replace(launch, env=env, unset=("LAUNCH_DROP",))


class OtherAdapter(fake_adapter.FakeAdapter):
    session_env = "OTHER_SESSION"
    identity_env = ("OTHER_SESSION", "OTHER_ID")
    child_defaults = {"OTHER_MODE": "on"}


class EnvAdapter(ProbeAdapter):
    """The probe adapter whose session launches carry spec.access.env and spec.env, as claude's and
    codex's do.
    """

    def headless(self):
        return EnvRun(self)

    def tui(self):
        return EnvTui(self)


class EnvRun(fake_adapter.FakeHeadless):
    def launch(self, prompt, request: SessionRequest):
        spec = request.spec
        LAUNCHED.append(spec)
        launch = super().launch(prompt, request)
        return dataclasses.replace(launch, env={**spec.access.env, **spec.env})


class EnvTui(fake_adapter.FakeTui):
    def launch(self, prompt, request: SessionRequest, hook):
        spec = request.spec
        launch = super().launch(prompt, request, hook)
        return dataclasses.replace(launch, env={**spec.access.env, **spec.env})


def package(name, cls) -> types.ModuleType:
    """An in-memory module agent_pm.session.adapters.<name> holding `cls`."""
    module = types.ModuleType(f"agent_pm.session.adapters.{name}")
    setattr(module, cls.__name__, cls)
    return module


PACKAGES = {
    "fake": fake_adapter,
    **{
        name: package(name, cls)
        for name, cls in (
            ("blank", BlankAdapter),
            ("failing", FailingAdapter),
            ("quiet", QuietAdapter),
            ("probe", ProbeAdapter),
            ("checked", CheckedAdapter),
            ("inner", InnerAdapter),
            ("other", OtherAdapter),
            ("env", EnvAdapter),
        )
    },
}


class Base(Case):
    def setUp(self):
        self.probe_dir = os.path.join(self.agent_pm, "probe")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.workdir = os.path.join(self.dir, "work")
        self.scenario = os.path.join(self.dir, "scenario.json")
        self.log = os.path.join(self.dir, "runtime.log")
        self.steps()
        for name, m in PACKAGES.items():
            self.enterContext(module_slot(f"agent_pm.session.adapters.{name}", m))
        self.configure()

    def steps(self, *steps):
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(list(steps), f)

    def configure(self, runtime="fake", settings=None, tables=None, **extra):
        """A table per PACKAGES name, each with this test's scenario and log, plus `tables`; `extra`
        keys go into runtime's; settings: the config's global keys.
        """
        tables = {
            **{
                name: {
                    "implementation": f"adapters.{name}:{name.capitalize()}Adapter",
                    "scenario": self.scenario,
                    "log": self.log,
                }
                for name in PACKAGES
            },
            **(tables or {}),
        }
        tables[runtime].update(extra)
        self.configure_session(tables, settings or {})

    def spec(self, **fields) -> Spec:
        return Spec(workdir=self.workdir, **fields)

    def start(
        self, runtime="fake", mode="headless", prompt="hello", spec=None, **kwargs
    ) -> api.Handle:
        """`sid` and `name` go into the spec, as self.driver.start takes them."""
        given = {k: kwargs.pop(k) for k in ("sid", "name") if k in kwargs}
        spec = dataclasses.replace(spec or self.spec(), **given)
        handle = self.driver.start(runtime, mode, prompt, spec, options=StartOptions(**kwargs))
        self.addCleanup(handle.stop)
        return handle

    def drain(self, handle) -> list:
        """handle.events() to its end; fails, stopping the handle, instead of hanging past WAIT."""
        found, errors = [], []

        def run():
            try:
                found.extend(handle.events())
            except BaseException as e:
                errors.append(e)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(WAIT)
        if thread.is_alive():
            handle.stop()
            self.fail(f"events() did not end within {WAIT}s")
        if errors:
            raise errors[0]
        return found

    def run_once(self, **kwargs) -> tuple[api.Handle, list[str]]:
        """A started session run to its end, and the argv its runtime logged."""
        handle = self.start(**kwargs)
        self.assertEqual(self.drain(handle), [Exited(0)])
        with open(self.log, encoding="utf-8") as f:
            (entry,) = [json.loads(line) for line in f]
        return handle, entry["argv"]

    def written(self) -> list[dict]:
        return records.read(records.path(self.workdir))

    def refused(self, error, message, call):
        """call() raises `error` with `message`, nothing started and no run record written."""
        with mock.patch("subprocess.Popen", side_effect=AssertionError("started")) as popen:
            with self.assertRaises(error) as caught:
                call()
        self.assertEqual(str(caught.exception), message)
        popen.assert_not_called()
        self.assertFalse(os.path.exists(records.path(self.workdir)))
        self.assertFalse(os.path.exists(self.log))


class ChecksTest(Base):
    def test_refuses_an_unknown_mode(self):
        for bad in ("pane", "", "Headless", None):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f"mode must be one of headless, tui, in_process, got {bad!r}",
                    lambda *, bad=bad: self.start(mode=bad),
                )

    def test_refuses_an_idle_timeout_that_is_no_positive_number(self):
        for bad in (0, -1, 0.0, -0.5, float("nan"), True, False, "1"):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f"idle_timeout must be a positive number of seconds, got {bad!r}",
                    lambda *, bad=bad: self.start(idle_timeout=bad),
                )

    def test_refuses_a_resume_that_is_no_session_id(self):
        for bad in ("", 1, b"x" * 100):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f"resume must be a session id, got {bad!r:.80}",
                    lambda *, bad=bad: self.start(resume=bad),
                )

    def test_refuses_a_sid_that_is_not_a_non_empty_printable_string_without_a_slash(self):
        for bad, shown in (
            ("", "''"),
            ("a/b", "'a/b'"),
            ("a\tb", r"'a\tb'"),
            (5, "5"),
            ("s" * 200 + "\n", "'" + "s" * 79),
        ):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f'sid must be a non-empty printable string without "/", got {shown}',
                    lambda *, bad=bad: self.start(sid=bad),
                )

    def test_refuses_a_sid_other_than_resume(self):
        self.refused(
            ConfigError,
            "sid 's1' differs from resume 'r1'",
            lambda: self.start(sid="s1", resume="r1"),
        )

    def test_the_spec_sid_is_the_sessions_and_may_be_resumes(self):
        for kwargs in ({}, {"resume": "s1"}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.start(spec=self.spec(sid="s1"), **kwargs).sid, "s1")

    def test_a_spec_name_is_tui_only(self):
        for runtime, mode in (("fake", "headless"), ("checked", "in_process")):
            with self.subTest(mode=mode):
                self.refused(
                    ConfigError,
                    "name: tui mode only",
                    lambda *, mode=mode, runtime=runtime: self.start(
                        runtime, mode, spec=self.spec(name="w1")
                    ),
                )

    def test_protect_is_tui_only(self):
        for runtime, mode in (("fake", "headless"), ("checked", "in_process")):
            with self.subTest(mode=mode):
                self.refused(
                    ConfigError,
                    "protect: tui mode only",
                    lambda *, mode=mode, runtime=runtime: self.start(
                        runtime, mode, spec=self.spec(protect=("/p",))
                    ),
                )

    def test_headless_flags_and_env_reach_the_adapter_and_child(self):
        LAUNCHED.clear()
        spec = self.spec(flags=("--custom", "value"), env={"FAKE_ENV": "spec"})
        handle, _ = self.run_once(runtime="env", spec=spec)
        self.assertEqual(
            (LAUNCHED[0].flags, dict(LAUNCHED[0].env), LAUNCHED[0].sid),
            (spec.flags, dict(spec.env), handle.sid),
        )
        with open(self.log, encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readline())["env"]["FAKE_ENV"], "spec")

    def test_session_selection_flags_are_refused_before_launch(self):
        from agent_pm.session.adapters.claude import ClaudeAdapter
        from agent_pm.session.adapters.codex import CodexAdapter

        for runtime, adapter in (("claude", ClaudeAdapter), ("codex", CodexAdapter)):
            self.configure(
                tables={runtime: {"implementation": f"adapters.{runtime}:{adapter.__name__}"}}
            )
            flags = (
                *adapter.forbidden_selection,
                *(f"{word}=x" for word in adapter.forbidden_selection if word.startswith("--")),
            )
            for flag in flags:
                with self.subTest(runtime=runtime, flag=flag):
                    self.refused(
                        ConfigError,
                        f"{flag}: picks the session; use sid= or resume=",
                        lambda *, flag=flag, runtime=runtime: self.start(
                            runtime=runtime, spec=self.spec(flags=("--verbose", flag, SID))
                        ),
                    )

    def test_selection_scanning_stops_at_the_separator_and_accepts_other_words(self):
        forbidden = ("--resume", "-r", "resume", "fork", "--last")
        for flags in (("--", *forbidden), ("--resumes", "--continued", "--teleport-x")):
            with self.subTest(flags=flags):
                selection.check(flags, forbidden)

    def test_selection_words_used_as_option_values_are_not_selectors(self):
        from agent_pm.session.adapters.codex import CodexAdapter

        for flags in (
            ("--add-dir", "fork"),
            ("--add-dir=resume",),
            ("--enable", "hooks"),
            ("-c", "model=x"),
            ("-cmodel=x",),
        ):
            with self.subTest(flags=flags):
                selection.check(flags, CodexAdapter.forbidden_selection, CodexAdapter.valued_flags)
        with self.assertRaisesRegex(ConfigError, "fork: picks the session"):
            selection.check(
                ("--add-dir", "safe", "fork"),
                CodexAdapter.forbidden_selection,
                CodexAdapter.valued_flags,
            )

    def test_selection_uses_declared_syntax_and_uuid_validation(self):
        syntax = (("--session-id",), ("--resume",))
        self.assertEqual(
            selection.select(SID, syntax, uuid=True, resume=False), ("--session-id", SID)
        )
        self.assertEqual(selection.select(SID, syntax, uuid=True, resume=True), ("--resume", SID))
        self.assertEqual(selection.select("native", ((), ("resume",)), resume=False), ())
        self.assertEqual(
            selection.select("native", ((), ("resume",)), resume=True), ("resume", "native")
        )
        for native in ("abc", SID.upper(), SID + "\n"):
            with (
                self.subTest(native=native),
                self.assertRaisesRegex(ConfigError, "session ids are UUIDs"),
            ):
                selection.select(native, syntax, uuid=True, resume=False)

    def test_a_registry_error_passes_through(self):
        self.refused(
            ConfigError,
            "no [adapters.nope] table in the configured adapters",
            lambda: self.start(runtime="nope"),
        )
        self.configure(model="m")
        self.refused(
            ConfigError, "unknown key 'model' in FakeAdapter's config", lambda: self.start()
        )

    def test_tui_on_a_runtime_without_it_is_unsupported(self):
        self.refused(
            ConfigError,
            "runtime 'fake' does not support tui mode",
            lambda: self.start(mode="tui"),
        )

    def test_headless_takes_no_name_or_events(self):
        self.refused(ConfigError, "name: tui mode only", lambda: self.start(name="w1"))
        self.refused(ConfigError, "events: tui mode only", lambda: self.start(events=self.scenario))

    def test_refuses_a_launch_without_a_command(self):
        self.refused(
            ConfigError, "runtime 'blank' gave no command", lambda: self.start(runtime="blank")
        )

    def test_an_exception_from_launch_propagates(self):
        self.refused(LookupError, "no binding", lambda: self.start(runtime="failing"))

    def test_a_failed_session_record_propagates_before_the_start(self):
        os.mkdir(self.workdir)
        target = os.path.join(self.dir, "elsewhere")
        os.symlink(target, records.path(self.workdir))
        with mock.patch("subprocess.Popen", side_effect=AssertionError("started")) as popen:
            with self.assertRaises(OSError):
                self.start()
        popen.assert_not_called()
        self.assertFalse(os.path.exists(target))


class SpawnTest(Base):
    def test_a_spawn_failure_is_a_driver_error_after_the_session_record(self):
        for error in (OSError("no exec"), ValueError("bad argument")):
            with self.subTest(error=error):
                self.workdir = os.path.join(self.dir, type(error).__name__)
                with mock.patch("subprocess.Popen", side_effect=error) as popen:
                    with self.assertRaises(DriverError) as caught:
                        self.start()
                self.assertEqual(str(caught.exception), f"cannot start {sys.executable}: {error}")
                popen.assert_called_once()
                self.assertEqual([e["kind"] for e in self.written()], ["session"])


class SidTest(Base):
    def test_resume_continues_that_session(self):
        handle, argv = self.run_once(resume="r1")
        self.assertEqual(handle.sid, "r1")
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--sid") + 1], "r1")
        self.assertEqual(self.written()[0]["sid"], "r1")

    def test_sid_may_repeat_resume(self):
        handle, argv = self.run_once(sid="s1", resume="s1")
        self.assertEqual(handle.sid, "s1")
        self.assertIn("--resume", argv)

    def test_sid_names_a_new_session_and_reaches_launch(self):
        handle, argv = self.run_once(sid="s1")
        self.assertEqual((handle.sid, handle.info().sid), ("s1", "s1"))
        self.assertNotIn("--resume", argv)
        self.assertEqual(argv[argv.index("--sid") + 1], "s1")
        self.assertEqual(self.written()[0]["sid"], "s1")

    def test_neither_picks_a_uuid4(self):
        handle, argv = self.run_once()
        self.assertEqual(uuid.UUID(handle.sid).version, 4)
        self.assertEqual(str(uuid.UUID(handle.sid)), handle.sid)
        self.assertEqual(argv[argv.index("--sid") + 1], handle.sid)
        self.assertEqual(self.written()[0]["sid"], handle.sid)


class RuntimeNoteTest(Base):
    def test_with_commands_the_note_follows_the_prompt(self):
        _, argv = self.run_once(spec=self.spec(access=Access(commands=("git",))))
        self.assertEqual(argv[-1], "hello\n\nFAKE NOTE")

    def test_without_commands_the_prompt_is_unchanged(self):
        _, argv = self.run_once()
        self.assertEqual(argv[-1], "hello")

    def test_an_empty_note_leaves_the_prompt_unchanged(self):
        _, argv = self.run_once(runtime="quiet", spec=self.spec(access=Access(commands=("git",))))
        self.assertEqual(argv[-1], "hello")


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
                (p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()
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


class Clock:
    """time.monotonic's stand-in: `now` moves only when a StubMode waits."""

    now = 0.0


class StubMode:
    """A Mode on a Clock: poll(timeout) gives the next script item (at, events, ended) once the
    clock reaches `at`, else ([], False), moving the clock to `at` or on by timeout, whichever is
    sooner; past LATE s it raises, so a Handle that never ends fails. returncode() gives `code`;
    every call is logged in `calls`.
    """

    LATE = 60

    def __init__(self, clock, script, code):
        self.clock, self.script, self.code, self.calls = clock, list(script), code, []

    def poll(self, timeout):
        self.calls.append(("poll", timeout))
        if self.clock.now > self.LATE:
            raise AssertionError("polled past the script")
        if self.script and self.script[0][0] <= self.clock.now + timeout:
            at, events, ended = self.script.pop(0)
            self.clock.now = max(self.clock.now, at)
            return list(events), ended
        self.clock.now += timeout
        return [], False

    def returncode(self, timeout):
        self.calls.append(("returncode", timeout))
        return self.code

    def state(self):
        self.calls.append(("state",))
        return "unknown"

    def send(self, text):
        self.calls.append(("send", text))

    def screen(self, lines=None):
        self.calls.append(("screen", lines))
        return "pane"

    def stop(self):
        self.calls.append(("stop",))


class HandleOverAModeTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.clock = Clock()
        patch = mock.patch("time.monotonic", lambda: self.clock.now)
        patch.start()
        self.addCleanup(patch.stop)

    def handle(self, *script, code=0, idle_timeout=None, **kwargs) -> api.Handle:
        self.mode = StubMode(self.clock, script, code)
        return api.Handle(
            "fake",
            "tui",
            Spec(workdir=self.dir, sid="s1", name=kwargs.pop("name", None)),
            host=self.mode,
            idle_timeout=idle_timeout,
            info=SessionInfo("s1", "", self.dir, False, "", ""),
            **kwargs,
        )

    def events(self, handle) -> list:
        found = list(itertools.islice(handle.events(), 50))
        self.assertLess(len(found), 50, found)
        return found

    def test_binding_and_late_metadata_preserve_completion(self):
        adapter = fake_adapter.FakeAdapter({})
        resolver = mock.Mock(side_effect=lambda info: dataclasses.replace(info, resume="resume n1"))
        adapter.session_metadata = resolver
        handle = self.handle((0, [SessionBound("n1")], True), adapter=adapter)
        records.note(records.path(self.dir), "session", sid="s1", cwd=self.dir, project=False)
        self.assertEqual(self.events(handle), [SessionBound("n1"), Exited(0)])
        self.assertEqual(handle.info().resume, "resume n1")
        self.assertEqual(records.session_record(self.dir, "s1")["resume"], "resume n1")
        records.note(records.path(self.dir), "result", outcome={"status": "done"})
        resolver.side_effect = lambda info: dataclasses.replace(info, transcript="/late")
        self.assertEqual(handle.info().transcript, "/late")
        self.assertEqual(records.session_record(self.dir, "s1")["transcript"], "/late")
        self.assertEqual(
            sum(e["kind"] == "session" for e in records.read(records.path(self.dir))), 1
        )
        previous = handle.info()
        resolver.side_effect = OSError("metadata unavailable")
        self.assertEqual(handle.info(), previous)

    def test_binding_record_failure_remains_fatal(self):
        adapter = fake_adapter.FakeAdapter({})
        handle = self.handle((0, [SessionBound("n1")], True), adapter=adapter)
        with mock.patch("os.open", side_effect=OSError("binding failed")):
            with self.assertRaisesRegex(OSError, "binding failed"):
                self.events(handle)
        self.assertIn(("stop",), self.mode.calls)
        self.assertEqual(handle.info().native_id, "")

    def test_metadata_resolver_cannot_replace_session_identity_or_effective_project(self):
        adapter = fake_adapter.FakeAdapter({})
        adapter.session_metadata = lambda info: dataclasses.replace(
            info, sid="other", native_id="other", cwd="/other", project=True, transcript="/t"
        )
        handle = self.handle((0, [SessionBound("n1")], True), adapter=adapter)
        self.events(handle)
        self.assertEqual(handle.info(), SessionInfo("s1", "n1", self.dir, False, "/t", ""))

    def test_events_in_order_the_modes_exited_dropped_exited_last_in_poll_slices(self):
        handle = self.handle(
            (0, [Activity("a"), Exited(9)], False),
            (1, [TurnEnd("done")], False),
            (2, [Stderr("e")], True),
            code=3,
        )
        self.assertEqual(
            self.events(handle),
            [Activity("a"), TurnEnd("done"), Stderr("e"), Exited(3)],
        )
        self.assertEqual(self.events(handle), [])
        self.assertEqual(handle.reply, "done")
        self.assertEqual({call[1] for call in self.mode.calls if call[0] == "poll"}, {api.POLL})
        self.assertEqual(self.mode.calls[-1], ("returncode", api.POLL))

    def test_each_idle_span_yields_idle_timeout(self):
        handle = self.handle(
            (0, [Activity()], False),
            (5, [Reply("r")], False),
            (5.5, [], True),
            idle_timeout=2,
        )
        self.assertEqual(
            self.events(handle),
            [Activity(), IdleTimeout(2), IdleTimeout(2), Reply("r"), Exited(0)],
        )

    def test_after_blocked_no_idle_timeout_until_an_event_but_stderr(self):
        handle = self.handle(
            (0, [Blocked("ask")], False),
            (0.5, [Stderr("x")], False),
            (3, [Activity()], False),
            (4.5, [], True),
            idle_timeout=1,
        )
        self.assertEqual(
            self.events(handle),
            [
                Blocked("ask"),
                Stderr("x"),
                Activity(),
                IdleTimeout(1),
                Exited(0),
            ],
        )

    def test_an_exception_from_the_mode_stops_it_then_propagates(self):
        handle = self.handle()
        with mock.patch.object(self.mode, "poll", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                self.events(handle)
        self.assertEqual(self.mode.calls, [("stop",)])
        self.assertEqual(self.events(handle), [])

    def test_state_send_screen_and_stop_are_the_modes_and_name_defaults_to_none(self):
        handle = self.handle()
        self.assertIsNone(handle.name)
        self.assertEqual(
            (handle.state(), handle.screen(), handle.screen(5)), ("unknown", "pane", "pane")
        )
        handle.send("hi")
        handle.stop()
        self.assertEqual(
            self.mode.calls,
            [("state",), ("screen", None), ("screen", 5), ("send", "hi"), ("stop",)],
        )
        self.assertEqual(self.handle(name="w1").name, "w1")


class UsageTest(Base):
    def usage(self, out="42.5", **launch):
        """self.driver.usage("probe"), its probe the Launch of `launch` (argv ("probe-cmd", "-x")
        unless given) and headless.probe patched to give `out`; its result and the patch's mock.
        """
        self.configure("probe", probe={"argv": ("probe-cmd", "-x"), **launch})
        with mock.patch.object(self.driver, "probe", return_value=out) as probe:
            return self.driver.usage("probe"), probe

    def test_no_probe_is_none_and_runs_nothing(self):
        with mock.patch.object(self.driver, "probe", side_effect=AssertionError("probed")) as probe:
            self.assertIsNone(self.driver.usage("fake"))
        probe.assert_not_called()
        self.assertFalse(os.path.exists(self.probe_dir))

    def test_the_probes_stdout_as_the_adapter_parses_it(self):
        found, probe = self.usage("42.5")
        self.assertEqual(found, Usage(Window(42.5, None), None))
        probe.assert_called_once_with(
            ("probe-cmd", "-x"), cwd=self.probe_dir, env=mock.ANY, timeout=api.PROBE_TIMEOUT
        )

    def test_a_spawn_error_is_none(self):
        self.configure("probe", probe={"argv": ("probe-cmd",)})
        with mock.patch("subprocess.Popen", side_effect=OSError("no exec")) as popen:
            self.assertIsNone(self.driver.usage("probe"))
        popen.assert_called_once()

    def test_a_timeout_is_none(self):
        found, probe = self.usage(None)
        self.assertIsNone(found)
        self.assertEqual(probe.call_args.kwargs["timeout"], api.PROBE_TIMEOUT)

    def test_a_reading_without_windows_is_a_usage(self):
        with mock.patch.object(ProbeAdapter, "usage", return_value=engine_types.Usage(None, None)):
            found, probe = self.usage()
        self.assertEqual(found, Usage(None, None))
        probe.assert_called_once()

    def test_no_reading_is_none(self):
        with mock.patch.object(ProbeAdapter, "usage", return_value=None):
            self.assertIsNone(self.usage()[0])

    def test_an_exception_from_the_adapters_parse_is_none(self):
        for out in ("", "garbage"):
            with self.subTest(out=out):
                self.assertIsNone(self.usage(out)[0])

    def test_a_probe_without_a_command_is_none(self):
        found, probe = self.usage(argv=())
        self.assertIsNone(found)
        probe.assert_not_called()

    def test_the_probe_dir_is_made_0700_under_home(self):
        self.usage()
        mode = os.lstat(self.probe_dir).st_mode
        self.assertTrue(stat.S_ISDIR(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o700)
        found, probe = self.usage()
        self.assertEqual(found, Usage(Window(42.5, None), None))
        self.assertEqual(probe.call_args.kwargs["cwd"], self.probe_dir)

    def test_a_symlink_or_non_directory_at_the_probe_dir_is_none_and_runs_nothing(self):
        target = os.path.join(self.dir, "target")
        os.mkdir(target)
        for kind, make in (
            ("symlink", lambda: os.symlink(target, self.probe_dir)),
            ("file", lambda: open(self.probe_dir, "w").close()),
        ):
            with self.subTest(kind=kind):
                make()
                try:
                    found, probe = self.usage()
                finally:
                    os.remove(self.probe_dir)
                self.assertIsNone(found)
                probe.assert_not_called()

    def test_the_probe_gets_the_child_environment_in_launch_cwd(self):
        cwd = os.path.join(self.dir, "cwd")
        os.mkdir(cwd)
        with mock.patch.dict(os.environ, {"API_DROP": "x", "API_KEEP": "k"}):
            _, probe = self.usage(env={"API_ADD": "a"}, unset=("API_DROP",), cwd=cwd)
        env = probe.call_args.kwargs["env"]
        self.assertEqual(
            (env.get("API_DROP"), env["API_KEEP"], env["API_ADD"], env["PWD"]),
            (None, "k", "a", cwd),
        )
        self.assertEqual(probe.call_args.kwargs["cwd"], cwd)
        self.assertFalse(os.path.exists(self.probe_dir))

    def test_a_registry_error_raises(self):
        with self.assertRaises(ConfigError):
            self.driver.usage("nope")


class SessionInfoTest(Base):
    def test_the_adapters_session_status(self):
        self.assertEqual(
            self.driver.session_status("probe", "s1", self.dir),
            SessionStatus("yes", 1.5, f"probe s1 {self.dir}"),
        )

    def test_unknown_by_default(self):
        self.assertEqual(
            self.driver.session_status("fake", "s1", "/c"), SessionStatus("unknown", None, "")
        )

    def test_an_os_error_from_the_adapter_is_unknown(self):
        with mock.patch.object(
            ProbeAdapter, "session_status", side_effect=PermissionError(13, "denied")
        ):
            self.assertEqual(
                self.driver.session_status("probe", "s1", self.dir),
                SessionStatus("unknown", None, ""),
            )

    def test_refuses_a_bad_sid_or_cwd_and_passes_registry_errors(self):
        for sid, shown in (("", "''"), ("a/b", "'a/b'"), (5, "5")):
            with self.subTest(sid=sid), self.assertRaises(ConfigError) as caught:
                self.driver.session_status("probe", sid, self.dir)
            self.assertEqual(
                str(caught.exception),
                f'sid must be a non-empty printable string without "/", got {shown}',
            )
        for cwd, shown in (("rel", "'rel'"), ("", "''"), ("/c\n", r"'/c\n'"), (None, "None")):
            with self.subTest(cwd=cwd), self.assertRaises(ConfigError) as caught:
                self.driver.session_status("probe", "s1", cwd)
            self.assertEqual(
                str(caught.exception), f"cwd must be a printable absolute path, got {shown}"
            )
        with self.assertRaises(ConfigError) as caught:
            self.driver.session_status("nope", "s1", self.dir)
        self.assertEqual(
            str(caught.exception),
            "no [adapters.nope] table in the configured adapters",
        )


class WritableDirsTest(Base):
    def dirs(self, **kwargs):
        return mock.patch.object(ProbeAdapter, "writable_dirs", autospec=True, **kwargs)

    def test_the_fake_runtime_claims_none(self):
        self.assertEqual(self.driver.writable_dirs("fake", self.spec()), ())

    def test_the_adapters_dirs_as_a_tuple_under_the_spec_with_the_resumed_sid(self):
        spec = self.spec(access=Access(dirs=(self.dir,), cwd=self.dir), tier=2)
        with self.dirs(return_value=[self.dir, self.workdir]) as dirs:
            self.assertEqual(
                self.driver.writable_dirs("probe", spec, "r1"), (self.dir, self.workdir)
            )
            self.assertEqual(
                self.driver.writable_dirs("probe", spec),
                (self.dir, self.workdir),
            )
        self.assertEqual(
            [c.args[1:] for c in dirs.call_args_list],
            [
                (SessionRequest(dataclasses.replace(spec, sid="r1"), resumed=True),),
                (SessionRequest(spec, resumed=False),),
            ],
        )

    def test_refuses_a_spec_sid_a_bad_resume_or_an_unknown_runtime(self):
        set_sid = "spec.sid is set by writable_dirs(): pass resume="
        bad_sid = 'sid must be a non-empty printable string without "/", got '
        unknown = "no [adapters.nope] table in the configured adapters"
        cases = [
            ("s1", None, "probe", set_sid),
            ("s1", "s1", "probe", set_sid),
            *((None, bad, "probe", bad_sid + repr(bad)) for bad in ("", "a/b", 5)),
            (None, None, "nope", unknown),
        ]
        for spec_sid, resume, runtime, message in cases:
            with (
                self.subTest(spec_sid=spec_sid, resume=resume, runtime=runtime),
                self.dirs() as dirs,
                self.assertRaises(ConfigError) as caught,
            ):
                self.driver.writable_dirs(runtime, self.spec(sid=spec_sid), resume)
            self.assertEqual(str(caught.exception), message)
            dirs.assert_not_called()

    def test_an_os_error_from_the_adapter_is_a_config_error(self):
        with self.dirs(side_effect=PermissionError(13, "denied")):
            with self.assertRaises(ConfigError) as caught:
                self.driver.writable_dirs("probe", self.spec())
        self.assertEqual(str(caught.exception), "probe's writable dirs: [Errno 13] denied")
        self.assertIsInstance(caught.exception.__cause__, PermissionError)


def fake_host() -> mock.Mock:
    """tmux_host autospecced, its constants, NAME, TuiError, attach_command and events_file real,
    PAUSE 0: status None (absent), listing [], own_session None, option None, pane %1, tmux_path
    TMUX.
    """
    host = mock.create_autospec(tmux_host)
    for name in ("RUNNING", "UNKNOWN", "NAME", "OPENER", "SPLITS", "TuiError"):
        setattr(host, name, getattr(tmux_host, name))
    host.PAUSE = 0
    host.attach_command.side_effect = tmux_host.attach_command
    host.events_file.side_effect = tmux_host.events_file
    host.status.return_value = None
    host.listing.return_value = []
    host.own_session.return_value = None
    host.option.return_value = None
    host.pane.return_value = "%1"
    host.tmux_path.return_value = TMUX
    return host


class TuiBase(Base):
    def setUp(self):
        super().setUp()
        self.configure(tui=True)
        self.root = os.path.join(os.path.dirname(self.probe_dir), "tui")
        self.events = os.path.join(self.dir, "events")
        self.host = fake_host()
        self.driver_dependencies = {"host": self.host}
        self.driver.host = self.host

    def tui(self, **kwargs) -> api.Handle:
        return self.start(mode="tui", **kwargs)

    def nothing_hosted(self):
        self.assertEqual(self.host.mock_calls, [])
        self.assertFalse(os.path.lexists(self.root))

    def name_refused(self, call, bad=(*BAD_NAMES, None)):
        """call(name) refuses each bad name before any tmux call or path use."""
        for name in bad:
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                call(name)
            self.assertEqual(
                str(caught.exception), f"name must match [A-Za-z0-9_-]+, got {name!r:.80}"
            )
        self.nothing_hosted()


class TuiStartTest(TuiBase):
    def test_driver_binds_the_adapter_session_selector_for_new_and_resumed_sessions(self):
        self.configure(tables={"claude": {"implementation": "adapters.claude:ClaudeAdapter"}})
        for resumed, word in ((False, "--session-id"), (True, "--resume")):
            with self.subTest(resumed=resumed):
                self.tui(
                    runtime="claude",
                    name=f"w{resumed}",
                    **({"resume": SID} if resumed else {"sid": SID}),
                )
                argv = self.host.start.call_args.args[1]
                self.assertEqual(tuple(argv[:3]), ("claude", word, SID))

    def test_effective_project_override_reaches_info_record_and_tui_state(self):
        original = fake_adapter.FakeTui.launch
        for project in (True, False):

            def launch(run, *args, project=project):
                return dataclasses.replace(original(run, *args), project=project)

            with (
                self.subTest(project=project),
                mock.patch.object(fake_adapter.FakeTui, "launch", launch),
            ):
                handle = self.tui(
                    name=f"project-{project}", spec=self.spec(access=Access(project=not project))
                )
            self.assertIs(handle.info().project, project)
            self.assertIs(records.session_record(self.workdir, handle.sid)["project"], project)
            self.assertIs(tui.read_state(handle.name)["project"], project)

    def test_prepares_the_session_dir_records_the_session_then_begins_it_tagged_with_no_pane(self):
        order = []

        def status(name):
            order.append(("status", os.path.exists(records.path(self.workdir))))

        self.host.status.side_effect = status
        self.host.start.side_effect = lambda *a, **k: order.append(
            ("start", [e["kind"] for e in self.written()])
        )
        handle = self.tui(name="w1", sid=SID, events=self.events)
        self.assertEqual(order, [("status", False), ("start", ["session"])])
        self.assertEqual(
            [c[0] for c in self.host.mock_calls],
            ["events_file", "tmux_path", "status", "start", "pane"],
        )
        self.assertEqual(
            (handle.name, handle.sid, handle.runtime, handle.mode), ("w1", SID, "fake", "tui")
        )
        self.assertEqual(handle.info(), SessionInfo(SID, "", self.workdir, False, "", ""))
        (call,) = self.host.start.call_args_list
        self.assertEqual(call.args[0], "w1")
        self.assertEqual(
            call.args[1][-5:], ["hello", *tui.hook_argv(os.path.join(self.root, "w1"))]
        )
        self.assertEqual(
            {**call.kwargs, "env": None},
            {
                "cwd": self.workdir,
                "env": None,
                "events": self.events,
                "status_line": False,
                "tags": {"@sid": SID, "@runtime": "fake"},
            },
        )
        self.assertEqual(call.kwargs["env"]["PWD"], self.workdir)
        self.assertEqual(stat.S_IMODE(os.stat(self.events).st_mode), 0o600)
        self.assertEqual(
            {
                k: v
                for k, v in tui.read_state("w1").items()
                if k in ("runtime", "sid", "events", "words", "tmux_path")
            },
            {
                "runtime": "fake",
                "sid": SID,
                "events": self.events,
                "words": {"stop": "done", "ask": "blocked", "tool": "working"},
                "tmux_path": TMUX,
            },
        )

    def test_no_tmux_on_path_is_a_driver_error_with_nothing_started(self):
        self.host.tmux_path.return_value = None
        self.refused(DriverError, "tmux not found", lambda: self.tui(name="w1"))
        self.assertEqual(self.host.mock_calls, [mock.call.tmux_path()])
        self.assertFalse(os.path.lexists(self.root))

    def test_the_names_lock_is_held_from_prepare_to_begin(self):
        held = []

        def locked(*args, **kwargs):
            fd = os.open(os.path.join(self.root, "w1.lock"), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)

        self.host.status.side_effect = self.host.start.side_effect = locked
        self.tui(name="w1")
        locked()
        self.host.start.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(DriverError):
            self.tui(name="w1")
        locked()
        self.assertEqual(held, [True, True, False, True, False])

    def test_the_default_name_is_the_runtime_and_the_sids_first_8_chars(self):
        for kwargs in ({"sid": SID}, {"resume": SID}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.tui(**kwargs).name, "fake-0f8fad5b")
                self.assertEqual(self.host.start.call_args.args[0], "fake-0f8fad5b")
        handle = self.tui()
        self.assertEqual(handle.name, f"fake-{handle.sid[:8]}")

    def test_a_bad_name_given_or_made_from_the_sid_is_refused_before_tmux_or_a_path(self):
        self.name_refused(lambda name: self.tui(name=name), BAD_NAMES[:-1])
        self.refused(ConfigError, "name must be None or a string", lambda: self.tui(name=5))
        with self.assertRaises(ConfigError) as caught:
            self.tui(sid="a b")
        self.assertEqual(str(caught.exception), "name must match [A-Za-z0-9_-]+, got 'fake-a b'")
        self.nothing_hosted()
        self.assertFalse(os.path.exists(records.path(self.workdir)))

    def test_a_bad_events_file_is_refused(self):
        os.mkdir(self.events)
        sharp = os.path.join(self.dir, "a#b")
        for bad, message in (
            (self.events, f"events file {self.events}: Is a directory"),
            (sharp, f"events file {sharp}: tmux would misread it"),
            (5, "events must be a printable path, got 5"),
            ("/a\nb", r"events must be a printable path, got '/a\nb'"),
        ):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError, message, lambda *, bad=bad: self.tui(name="w1", events=bad)
                )
        self.host.status.assert_not_called()
        self.assertFalse(os.path.lexists(self.root))

    def test_a_running_or_unknown_session_is_refused_with_no_record(self):
        for status, message in (
            (tmux_host.RUNNING, "tui session w1 is running; tmux attach -t '=w1'"),
            (tmux_host.UNKNOWN, "tui session w1: its tmux state is unknown"),
        ):
            with self.subTest(status=status):
                self.host.status.return_value = status
                self.refused(DriverError, message, lambda: self.tui(name="w1"))
        self.host.start.assert_not_called()

    def test_a_failed_tmux_start_is_a_driver_error_after_the_record_removing_the_session_dir(self):
        self.host.start.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(DriverError) as caught:
            self.tui(name="w1")
        self.assertEqual(str(caught.exception), "cannot start tui session w1: tmux: boom")
        self.assertEqual([e["kind"] for e in self.written()], ["session"])
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))

    def test_the_status_line_is_the_tui_settings(self):
        self.configure(tui=True, settings={"status_line": True})
        self.tui(name="w1")
        self.assertIs(self.host.start.call_args.kwargs["status_line"], True)

    def test_a_bad_tui_setting_is_refused_before_anything(self):
        self.configure(tui=True, settings={"status_line": "yes"})
        self.refused(ConfigError, "status_line: want true or false", lambda: self.tui(name="w1"))
        self.nothing_hosted()

    def test_the_handles_state_screen_send_and_stop_are_the_tui_modes(self):
        handle = self.tui(name="w1")
        self.host.status.return_value = tmux_host.RUNNING
        self.host.read.return_value = "pane text"
        self.assertEqual((handle.state(), handle.screen()), ("live", "pane text"))
        handle.send("hi")
        self.host.paste.assert_called_once_with("%1", "hi")
        self.host.keys.assert_called_once_with("%1", "Enter")
        handle.stop()
        self.host.kill.assert_called_once_with("%1")
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))


class EnvironmentTest(TuiBase):
    def setUp(self):
        super().setUp()
        LAUNCHED.clear()

    def environ(self, **values):
        patch = mock.patch.dict(os.environ, {"HOME": os.environ["HOME"], **values}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def env(self, name, **fields) -> dict[str, str]:
        self.tui(runtime="inner", name=name, spec=self.spec(**fields))
        env = dict(self.host.start.call_args.kwargs["env"])
        self.assertEqual((env.pop("HOME"), env.pop("PWD")), (os.environ["HOME"], self.workdir))
        return env

    def test_spec_name_is_the_session_name_and_protect_gains_the_hook_paths(self):
        self.tui(runtime="inner", name="w1", spec=self.spec(protect=("/c",)), events=self.events)
        self.tui(runtime="inner", sid=SID)
        first, second = LAUNCHED
        self.assertEqual((first.name, first.protect), ("w1", ("/c", TMUX, self.events, self.root)))
        self.assertEqual(
            (second.name, second.protect, second.sid), ("inner-0f8fad5b", (TMUX, self.root), SID)
        )

    def test_every_session_drops_adapter_identities_and_merges_session_and_launch_env(self):
        self.environ(INNER_ID="i", OTHER_SESSION="", OTHER_ID="o", LAUNCH_DROP="x", KEEP="k")
        self.assertEqual(self.env("w1"), {"KEEP": "k", "LAUNCH_SET": "1"})
        flags = ("--custom", "v")
        self.assertEqual(
            self.env(
                "w2", flags=flags, env={"PAGER": "less", "OTHER_MODE": "off", "LAUNCH_SET": "spec"}
            ),
            {"KEEP": "k", "LAUNCH_SET": "1", "PAGER": "less", "OTHER_MODE": "off"},
        )
        self.assertEqual(LAUNCHED[-1].flags, flags)

    def test_a_child_default_is_dropped_only_inside_its_runtime_and_at_its_value(self):
        self.environ(PAGER="cat", NO_COLOR="1", OTHER_MODE="on")
        both = {"PAGER": "cat", "NO_COLOR": "1", "OTHER_MODE": "on", "LAUNCH_SET": "1"}
        self.assertEqual(self.env("w1"), both)
        self.environ(INNER_SESSION="1", PAGER="cat", NO_COLOR="0", OTHER_MODE="on")
        self.assertEqual(self.env("w2"), {"NO_COLOR": "0", "OTHER_MODE": "on", "LAUNCH_SET": "1"})
        self.environ(OTHER_SESSION="1", PAGER="cat", NO_COLOR="1", OTHER_MODE="on")
        self.assertEqual(self.env("w3"), {"PAGER": "cat", "NO_COLOR": "1", "LAUNCH_SET": "1"})
        self.assertEqual(self.env("w4", env={"OTHER_MODE": "on"}), both)

    def test_codex_injected_defaults_drop_only_inside_codex_at_their_injected_value(self):
        self.configure(tables={"codex": {"implementation": "adapters.codex:CodexAdapter"}})
        inside = {"CODEX_THREAD_ID": "t"}
        for environ, dropped in (
            ({"CLAUDECODE": "1", "NO_COLOR": "1", "PAGER": "cat"}, set()),
            ({**inside, "NO_COLOR": "0", "PAGER": "less", "CODEX_CI": "x"}, set(inside)),
            (
                {**inside, "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat", "CODEX_CI": "1"},
                {*inside, "NO_COLOR", "PAGER", "GIT_PAGER", "CODEX_CI"},
            ),
        ):
            with self.subTest(environ=environ):
                self.environ(**environ)
                self.assertEqual(set(environ) & api._identity_drop(self.driver.registry), dropped)

    def test_session_env_keys_need_no_adapter_allowlist(self):
        self.environ(KEEP="k")
        self.assertEqual(
            self.env("w1", env={"EDITOR": "vi", "ZED": "1"}),
            {"KEEP": "k", "LAUNCH_SET": "1", "EDITOR": "vi", "ZED": "1"},
        )

    def test_an_adapter_failing_to_load_refuses_every_start(self):
        self.configure(
            tui=True, tables={"broken": {"implementation": "adapters.broken:BrokenAdapter"}}
        )
        self.refused(
            ConfigError,
            "adapter 'broken': agent_pm.session.adapters.broken:BrokenAdapter: cannot import "
            "agent_pm.session.adapters.broken: No module named 'agent_pm.session.adapters.broken'",
            lambda: self.tui(runtime="inner", name="w1"),
        )
        self.host.start.assert_not_called()


class SessionEnvTest(TuiBase):
    def setUp(self):
        super().setUp()
        self.configure("env", probe={"argv": ("probe-cmd",)})
        self.enterContext(mock.patch.dict(os.environ, {"PYTHONPATH": "/parent"}))
        self.access = Access(commands=("git",), env={"PYTHONPATH": PYTHONPATH})

    def test_a_headless_session_gets_it_and_its_usage_probe_does_not(self):
        handle, _ = self.run_once(runtime="env", spec=self.spec(access=self.access))
        with open(self.log, encoding="utf-8") as f:
            (entry,) = [json.loads(line) for line in f]
        self.assertEqual(entry["env"]["PYTHONPATH"], PYTHONPATH)
        with mock.patch.object(self.driver, "probe", return_value="7") as probe:
            self.assertEqual(handle.usage(), Usage(Window(7.0, None), None))
        self.assertEqual(probe.call_args.kwargs["env"]["PYTHONPATH"], "/parent")

    def test_a_tui_session_gets_it(self):
        self.tui(runtime="env", name="w1", spec=self.spec(access=self.access))
        (call,) = self.host.start.call_args_list
        self.assertEqual(call.kwargs["env"]["PYTHONPATH"], PYTHONPATH)


class AttachTest(TuiBase):
    def started(self, *hooked) -> None:
        """Tui session w1 started (sid SID) on pane %1, then a hooks.jsonl record per (label,
        payload) in `hooked`, its @sid SID; the host's calls cleared.
        """
        self.tui(name="w1", sid=SID, events=self.events)
        with open(os.path.join(self.root, "w1", "hooks.jsonl"), "w") as f:
            f.writelines(
                json.dumps({"ts": "", "label": label, "payload": payload}) + "\n"
                for label, payload in hooked
            )
        self.host.option.side_effect = lambda target, key: (
            SID if (target, key) == ("%1", "@sid") else None
        )
        self.host.reset_mock()

    def test_a_handle_of_the_session_with_its_reply_native_id_and_next_records_events(self):
        self.started(("tool", {"session_id": "n1"}), ("stop", {"last_message": "r1"}))
        handle = self.driver.attach("w1", idle_timeout=2)
        self.assertEqual(
            (handle.name, handle.sid, handle.runtime, handle.mode, handle.reply),
            ("w1", SID, "fake", "tui", "r1"),
        )
        self.assertEqual(handle.info(), SessionInfo(SID, "n1", self.workdir, False, "", ""))
        self.assertEqual(
            self.host.mock_calls,
            [
                mock.call.pane("w1"),
                mock.call.option("%1", "@sid"),
                mock.call.option("%1", "@state"),
            ],
        )
        with open(os.path.join(self.root, "w1", "hooks.jsonl"), "a") as f:
            f.write(
                json.dumps({"ts": "", "label": "stop", "payload": {"last_message": "r2"}}) + "\n"
            )
        self.assertEqual(next(handle.events()), TurnEnd("r2", 0))
        self.assertEqual(handle.reply, "r2")

    def test_attach_refreshes_bound_metadata_without_starting_a_session(self):
        self.started(("tool", {"session_id": "n1"}))
        records.note(records.path(self.workdir), "result", outcome={"status": "done"})

        def metadata(adapter, info):
            self.assertEqual(info.native_id, "n1")
            return dataclasses.replace(info, transcript="/attached", resume="resume n1")

        with mock.patch.object(fake_adapter.FakeAdapter, "session_metadata", metadata):
            handle = self.driver.attach("w1")
        self.assertEqual(handle.info().transcript, "/attached")
        self.assertEqual(records.session_record(self.workdir, SID)["resume"], "resume n1")
        self.assertEqual(sum(e["kind"] == "session" for e in self.written()), 1)

    def test_attached_while_blocked_no_idle_timeout_until_its_next_event(self):
        """On a fake clock (time.monotonic, time.sleep), a tool record comes at 10 s."""
        hooks = os.path.join(self.root, "w1", "hooks.jsonl")
        for hooked, state, first in (
            ((("tool", {}), ("ask", {})), None, (Activity(""), 10)),
            ((), "blocked", (Activity(""), 10)),
            ((("ask", {}), ("tool", {})), None, (IdleTimeout(1), 1)),
            ((("ask", {}), ("stop", {})), "working", (IdleTimeout(1), 1)),
        ):
            with self.subTest(hooked=hooked, state=state):
                self.host.status.return_value = None
                self.started(*hooked)
                options = {"@sid": SID, "@state": state}
                self.host.option.side_effect = lambda target, key, *, options=options: options[key]
                self.host.status.return_value, self.host.read.return_value = tmux_host.RUNNING, ""
                now = [0.0]

                def sleep(seconds, *, now=now):
                    if now[0] > 60:
                        raise AssertionError("no event")
                    now[0] += seconds
                    if now[0] - seconds < 10 <= now[0]:
                        with open(hooks, "a") as f:
                            f.write(json.dumps({"ts": "", "label": "tool", "payload": {}}) + "\n")

                with (
                    mock.patch("time.monotonic", lambda *, now=now: now[0]),
                    mock.patch("time.sleep", sleep),
                ):
                    event = next(self.driver.attach("w1", idle_timeout=1).events())
                self.assertEqual((event, round(now[0])), first)

    def test_a_bad_name_or_idle_timeout_is_refused_before_tmux_or_a_path(self):
        self.name_refused(self.driver.attach)
        for bad in (0, -1, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as caught:
                self.driver.attach("w1", idle_timeout=bad)
            self.assertEqual(
                str(caught.exception),
                f"idle_timeout must be a positive number of seconds, got {bad!r}",
            )
        self.nothing_hosted()

    def test_no_session_dir_or_another_sid_is_no_tui_session(self):
        with self.assertRaises(DriverError) as caught:
            self.driver.attach("w1")
        self.assertTrue(str(caught.exception).startswith("no tui session w1: "), caught.exception)
        self.nothing_hosted()
        self.started()
        for found in ("other", None):
            with self.subTest(found=found), self.assertRaises(DriverError) as caught:
                self.host.option.side_effect = lambda name, key, *, found=found: found
                self.driver.attach("w1")
            self.assertEqual(str(caught.exception), "no tui session w1")

    def test_a_runtime_without_tui_or_a_registry_error_is_a_driver_error(self):
        self.started()
        self.configure(tui=False)
        for message in (
            "no tui session w1: runtime 'fake' has no tui mode",
            "no tui session w1: no [adapters.fake] table in the configured adapters",
        ):
            with self.subTest(message=message), self.assertRaises(DriverError) as caught:
                self.driver.attach("w1")
            self.assertEqual(str(caught.exception), message)
            self.configure_session({})
        self.host.option.assert_not_called()


def listed(name, **fields) -> dict:
    """A tmux_host.listing row of a live session with no tags, but `fields`."""
    return {
        "name": name,
        "sid": None,
        "runtime": None,
        "state": None,
        "pane": None,
        "opener": None,
        "dead": False,
        "shown": None,
        "last_lines": None,
        **fields,
    }


class SessionsTest(TuiBase):
    def test_a_row_per_listed_session_in_its_order(self):
        self.host.listing.return_value = [
            listed("mgr"),
            listed("w1", sid=SID, runtime="fake", state="done", pane="%3", opener="mgr", shown="m"),
            listed("w2", sid="a/b", runtime="Fake", state="blocked", shown=""),
            listed("w3", runtime="x-y", state="dead"),
            listed("w4", runtime="fake", state="done", dead=True, last_lines="bye"),
            listed("w5", runtime="fake9_", state="other", dead=None),
        ]
        self.host.own_session.return_value = "mgr"
        row = SessionRow
        self.assertEqual(
            self.driver.sessions(),
            [
                row("mgr", None, None, "working", None, None, None, None, True),
                row("w1", SID, "fake", "done", "%3", "mgr", "m", None, False),
                row("w2", None, None, "blocked", None, None, "", None, False),
                row("w3", None, None, "working", None, None, None, None, False),
                row("w4", None, "fake", "dead", None, None, None, "bye", False),
                row("w5", None, "fake9_", "working", None, None, None, None, False),
            ],
        )
        self.assertEqual(self.host.mock_calls, [mock.call.listing(None), mock.call.own_session()])

    def test_match_is_compiled_for_the_listing_to_fullmatch(self):
        self.assertEqual(self.driver.sessions(r"w[0-9]+"), [])
        ((pattern,),) = [c.args for c in self.host.listing.call_args_list]
        self.assertEqual(pattern.pattern, r"w[0-9]+")
        self.assertEqual(
            [n for n in ("w1", "w12", "xw1", "w1x") if pattern.fullmatch(n)], ["w1", "w12"]
        )

    def test_no_server_is_no_rows_without_reading_tmux_pane(self):
        self.assertEqual(self.driver.sessions(), [])
        self.host.own_session.assert_not_called()

    def test_a_non_str_or_invalid_match_is_refused(self):
        for bad, message in (
            (5, "match must be a regular expression, got 5"),
            (b"w", "match must be a regular expression, got b'w'"),
            ("(", "match '(': missing ), unterminated subpattern at position 0"),
            ("[", "match '[': unterminated character set at position 0"),
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as caught:
                self.driver.sessions(bad)
            self.assertEqual(str(caught.exception), message)
        self.nothing_hosted()

    def test_tmux_failing_or_a_bad_tmux_pane_is_a_driver_error_never_no_rows(self):
        self.host.listing.return_value = None
        with self.assertRaises(DriverError) as caught:
            self.driver.sessions()
        self.assertEqual(str(caught.exception), "tmux sessions unknown: tmux failed")
        self.host.listing.return_value = [listed("w1")]
        self.host.own_session.side_effect = tmux_host.TuiError("bad $TMUX_PANE 'x'")
        with self.assertRaises(DriverError) as caught:
            self.driver.sessions()
        self.assertEqual(str(caught.exception), "tmux sessions unknown: bad $TMUX_PANE 'x'")

    def test_a_row_is_frozen_with_these_fields(self):
        self.assertEqual(
            [f.name for f in dataclasses.fields(SessionRow)],
            ["name", "sid", "runtime", "state", "pane", "opener", "shown", "last_lines", "own"],
        )
        found = SessionRow("w1", None, None, "working", None, None, None, None, False)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            found.state = "done"


class CloseTest(TuiBase):
    def test_kills_the_session_and_removes_its_dir(self):
        self.tui(name="w1")
        self.host.reset_mock()
        self.driver.close("w1")
        self.assertEqual(self.host.mock_calls, [mock.call.kill("w1")])
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))
        self.assertTrue(os.path.isdir(self.root))

    def test_a_bad_name_is_refused_before_tmux_or_a_path(self):
        self.name_refused(self.driver.close)


PLACEMENTS = (
    ({"split": "left"}, "split must be right or below, got 'left'"),
    ({"split_from": "a b"}, "split_from must match [A-Za-z0-9_-]+, got 'a b'"),
    ({"split_from": 5}, "split_from must match [A-Za-z0-9_-]+, got 5"),
)
OPENERS = (
    ("x y", "opener must be a tmux session name or $ITERM_SESSION_ID's value, got 'x y'"),
    (
        "w0t0p0:a/b",
        "opener must be a tmux session name or $ITERM_SESSION_ID's value, got 'w0t0p0:a/b'",
    ),
)
NO_PANE = "no anchor pane: not in tmux, no iTerm2 pane ($ITERM_SESSION_ID)"


class ShowTest(TuiBase):
    def setUp(self):
        super().setUp()
        self.host.status.return_value = tmux_host.RUNNING
        self.host.open_pane.return_value = None

    def test_none_once_a_pane_shows_it_placed_with_the_tui_settings(self):
        self.configure(tui=True, settings={"workers_per_column": 2, "grid_retile": "off"})
        self.assertIsNone(
            self.driver.show("w1", split="below", split_from="m1", opener="w0t0p0:AB-12")
        )
        self.assertEqual(
            self.host.mock_calls,
            [
                mock.call.status("w1"),
                mock.call.open_pane(
                    "w1",
                    split="below",
                    split_from="m1",
                    opener="w0t0p0:AB-12",
                    per_column=2,
                    retile="off",
                ),
            ],
        )

    def test_no_pane_is_the_attach_command_the_reason_logged(self):
        self.host.status.return_value = 0
        self.host.open_pane.return_value = f"{NO_PANE}; watch it with tmux attach -t '=w1'"
        with (
            mock.patch.dict(os.environ, {"TUI_ATTACH_PREFIX": "docker exec -it box"}),
            self.assertLogs("driver") as logs,
        ):
            self.assertEqual(self.driver.show("w1"), "docker exec -it box tmux attach -t '=w1'")
        self.assertEqual(
            logs.output, [f"WARNING:driver:show w1: {NO_PANE}; watch it with tmux attach -t '=w1'"]
        )
        self.host.open_pane.assert_called_once_with(
            "w1", split=None, split_from=None, opener=None, per_column=None, retile=None
        )

    def test_an_absent_session_is_a_driver_error(self):
        self.host.status.return_value = None
        with self.assertRaises(DriverError) as caught:
            self.driver.show("w1")
        self.assertEqual(str(caught.exception), "no tmux session w1")
        self.host.open_pane.assert_not_called()

    def test_a_bad_name_placement_opener_or_setting_is_refused_before_tmux(self):
        self.name_refused(self.driver.show)
        for kwargs, message in (*PLACEMENTS, *(({"opener": o}, m) for o, m in OPENERS)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigError) as caught:
                self.driver.show("w1", **kwargs)
            self.assertEqual(str(caught.exception), message)
        self.configure(tui=True, settings={"grid_retile": "never"})
        with self.assertRaises(ConfigError) as caught:
            self.driver.show("w1")
        self.assertEqual(str(caught.exception), "grid_retile: want one of all, open-close, off")
        self.nothing_hosted()


class LayoutTest(TuiBase):
    def test_the_callers_opener_once_its_pane_is_found(self):
        self.host.opener.return_value = "mgr"
        self.assertEqual(self.driver.layout(), "mgr")
        self.assertEqual(self.host.mock_calls, [mock.call.anchor(None), mock.call.opener()])

    def test_beside_a_live_split_from_the_opener_may_be_none(self):
        self.host.status.return_value = tmux_host.RUNNING
        self.host.opener.side_effect = tmux_host.TuiError(NO_PANE)
        self.assertIsNone(self.driver.layout("below", "m1"))
        self.assertEqual(
            self.host.mock_calls,
            [mock.call.status("m1"), mock.call.anchor("m1"), mock.call.opener()],
        )

    def test_no_pane_or_no_opener_is_a_driver_error_with_a_hint_without_split_from(self):
        hint = ": run from tmux or iTerm2, or pass split_from"
        self.host.status.return_value = tmux_host.RUNNING
        for split_from, anchor, opener, message in (
            (None, NO_PANE, None, f"({NO_PANE}){hint}"),
            (None, None, "bad $ITERM_SESSION_ID 'x'", f"(bad $ITERM_SESSION_ID 'x'){hint}"),
            (
                "m1",
                "no terminal shows tmux session m1",
                None,
                "(no terminal shows tmux session m1)",
            ),
        ):
            with self.subTest(split_from=split_from, anchor=anchor):
                self.host.anchor.side_effect = anchor and tmux_host.TuiError(anchor)
                self.host.opener.side_effect = opener and tmux_host.TuiError(opener)
                with self.assertRaises(DriverError) as caught:
                    self.driver.layout(split_from=split_from)
                self.assertEqual(str(caught.exception), f"no pane to show the TUI beside {message}")

    def test_a_bad_split_or_split_from_or_an_absent_one_is_refused(self):
        for kwargs, message in PLACEMENTS:
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigError) as caught:
                self.driver.layout(**kwargs)
            self.assertEqual(str(caught.exception), message)
        self.nothing_hosted()
        with self.assertRaises(ConfigError) as caught:
            self.driver.layout(split_from="m1")
        self.assertEqual(str(caught.exception), "no tmux session m1")
        self.assertEqual(self.host.mock_calls, [mock.call.status("m1")])


class AttachCommandTest(TuiBase):
    def test_tmux_hosts_command_after_the_name_check(self):
        self.assertEqual(self.driver.attach_command("w1"), "tmux attach -t '=w1'")
        with mock.patch.dict(os.environ, {"TUI_ATTACH_PREFIX": "docker exec -it box"}):
            self.assertEqual(
                self.driver.attach_command("w1"), "docker exec -it box tmux attach -t '=w1'"
            )
        self.host.reset_mock()
        self.name_refused(self.driver.attach_command)


class DetachTest(TuiBase):
    def test_hosts_argv_with_the_signals_blocked_and_the_opener(self):
        env = {"PATH": "/bin", "TMUX": "x"}
        self.assertIsNone(self.driver.detach("d1", ["tool", "a b"], self.dir, env, opener="mgr"))
        self.assertEqual(
            self.host.mock_calls,
            [
                mock.call.detach(
                    "d1", ["tool", "a b"], cwd=self.dir, env=env, block=api.SIGNALS, opener="mgr"
                )
            ],
        )
        self.driver.detach("d2", ("tool",), self.dir, env)
        self.assertEqual(self.host.detach.call_args.kwargs["opener"], None)

    def test_a_bad_name_argv_cwd_or_opener_is_refused_before_tmux(self):
        self.name_refused(lambda name: self.driver.detach(name, ["tool"], self.dir, {}))
        missing, file = os.path.join(self.dir, "missing"), self.scenario
        for args, kwargs, message in (
            (([], self.dir), {}, "argv must be a non-empty list of str, got []"),
            (("tool", self.dir), {}, "argv must be a non-empty list of str, got 'tool'"),
            (([b"tool"], self.dir), {}, "argv must be a non-empty list of str, got [b'tool']"),
            ((["tool"], "work"), {}, "cwd must be a printable absolute path, got 'work'"),
            ((["tool"], "/a\nb"), {}, r"cwd must be a printable absolute path, got '/a\nb'"),
            *(
                ((["tool"], cwd), {}, f"cwd must be an existing directory, got {cwd!r:.80}")
                for cwd in (missing, file)
            ),
            *(((["tool"], self.dir), {"opener": o}, m) for o, m in OPENERS),
        ):
            with (
                self.subTest(args=args, kwargs=kwargs),
                self.assertRaises(ConfigError) as caught,
            ):
                self.driver.detach("d1", *args, {}, **kwargs)
            self.assertEqual(str(caught.exception), message)
        self.nothing_hosted()

    def test_a_tmux_failure_is_a_driver_error(self):
        self.host.detach.side_effect = tmux_host.TuiError("session d1 exists")
        with self.assertRaises(DriverError) as caught:
            self.driver.detach("d1", ["tool"], self.dir, {})
        self.assertEqual(str(caught.exception), "detach d1: session d1 exists")


class ExportsTest(Case):
    def test_api_exports_functions_and_constants_only(self):
        self.assertEqual(
            sorted(api.__all__),
            ["Driver", "Handle", "MODES", "SIGNALS", "holds", "note", "session_record"],
        )
        self.assertEqual([name for name in api.__all__ if not hasattr(api, name)], [])
        self.assertIs(api.session_record, records.session_record)

    def test_the_session_package_exports_them_not_the_module(self):
        for name in api.__all__:
            with self.subTest(name=name):
                self.assertIs(getattr(session_pkg, name), getattr(api, name))
                self.assertIn(name, session_pkg.__all__)
        self.assertNotIn("api", session_pkg.__all__)
        for name in session_pkg.__all__:
            if hasattr(engine_types, name):
                with self.subTest(name=name):
                    self.assertIs(getattr(session_pkg, name), getattr(engine_types, name))

    def test_constants(self):
        self.assertEqual(
            (api.MODES, api.STATES, api.POLL),
            (("headless", "tui", "in_process"), ("live", "absent", "unknown"), 0.5),
        )
        self.assertEqual((api.PROBE_DIR, api.PROBE_TIMEOUT), ("~/.agent-pm/probe", 120))
        self.assertEqual(api.SIGNALS, (signal.SIGHUP, signal.SIGTERM, signal.SIGINT))

    def test_note_appends_to_the_workdir_run_records(self):
        with tempfile.TemporaryDirectory() as d:
            api.note(d, "x", a=1)
            self.assertEqual(
                [(e["kind"], e["a"]) for e in records.read(records.path(d))], [("x", 1)]
            )


if __name__ == "__main__":
    unittest.main()
