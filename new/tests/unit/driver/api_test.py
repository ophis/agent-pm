"""new/core/src/driver/api.py: start()'s checks, its session record, sid and Runtime note, a headless Handle's surface,
usage() and session_info(), the session env (Access.env) reaching a session but not its usage probe, over the fake
adapter and test adapters registered in-process (sys.modules) with [adapters.<name>] tables given to
registry.configure. Wherever nothing may start, subprocess.Popen is patched to fail; usage()'s probe is headless.probe
patched, but for a spawn error. An in_process start over the in_process-only fake, SPAWNS patched to fail during its
run; claude and codex, from the committed config, refuse it. The Mode protocol, and a Handle over a scripted Mode on a
fake clock (time.monotonic patched). The tui paths (start, attach, sessions, close, show, layout, attach_command,
detach) over tmux_host autospecced (no tmux runs)."""

from new.tests import fake_adapter, hermetic

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

from agent_pm.adapters import base
from agent_pm.adapters.base import Access, Launch
from agent_pm.driver import api, errors, records, registry, tmux_host
from agent_pm.driver.modes import headless, in_process, tui
from agent_pm.utils.paths import IMPORT_ROOT

WAIT = 10  # upper bound on any wait, in seconds
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
BAD_NAMES = ("../x", "a b", "", "a/b", "w1\n", 5)
SPAWNS = ("subprocess.Popen", "os.fork", "os.posix_spawn", "os.posix_spawnp")


class BlankAdapter(fake_adapter.FakeAdapter):
    def headless(self):
        return BlankRun(self)


class BlankRun(fake_adapter.FakeHeadless):
    def launch(self, prompt, spec, resume):
        return Launch(())


class FailingAdapter(fake_adapter.FakeAdapter):
    def headless(self):
        return FailingRun(self)


class FailingRun(fake_adapter.FakeHeadless):
    def launch(self, prompt, spec, resume):
        raise LookupError("no binding")


class QuietAdapter(fake_adapter.FakeAdapter):
    runtime_note = ""


class CheckedAdapter(fake_adapter.InProcessFakeAdapter):
    def in_process(self):
        return CheckedRun(self)


class CheckedRun(fake_adapter.FakeInProcess):
    """check: refuses tier 4, else logs its arguments and whether run.jsonl exists."""

    def check(self, prompt, spec, resume):
        if spec.tier == 4:
            raise base.ConfigError("tier 4 is unmapped")
        with open(self.adapter.config["log"], "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "check": prompt,
                        "sid": spec.sid,
                        "resume": resume,
                        "recorded": os.path.exists(records.path(spec.workdir)),
                    }
                )
                + "\n"
            )


class ProbeAdapter(fake_adapter.FakeAdapter):
    """The fake adapter with a usage probe, its config's `probe` (Launch's fields), whose stdout is the 5-hour window's
    percent (anything else raises), and a session status naming its sid and cwd."""

    keys = fake_adapter.FakeAdapter.keys | {"probe"}

    def usage_probe(self):
        return Launch(**self.config["probe"])

    def usage(self, output):
        return base.Usage(base.Window(float(output), None), None)

    def session_status(self, sid, cwd):
        return base.SessionStatus("yes", 1.5, f"probe {sid} {cwd}")


class EnvAdapter(ProbeAdapter):
    """The probe adapter whose session launches carry spec.access.env, as claude's and codex's do."""

    def headless(self):
        return EnvRun(self)

    def tui(self):
        return EnvTui(self)


class EnvRun(fake_adapter.FakeHeadless):
    def launch(self, prompt, spec, resume):
        return dataclasses.replace(super().launch(prompt, spec, resume), env=spec.access.env)


class EnvTui(fake_adapter.FakeTui):
    def launch(self, prompt, spec, resume, hook):
        return dataclasses.replace(super().launch(prompt, spec, resume, hook), env=spec.access.env)


def package(name, cls) -> types.ModuleType:
    """An in-memory module agent_pm.adapters.<name> holding `cls`."""
    module = types.ModuleType(f"agent_pm.adapters.{name}")
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
            ("env", EnvAdapter),
        )
    },
}


class Base(unittest.TestCase):
    def setUp(self):
        self.probe_dir = os.path.join(hermetic.home(self), "probe")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.workdir = os.path.join(self.dir, "work")
        self.scenario = os.path.join(self.dir, "scenario.json")
        self.log = os.path.join(self.dir, "runtime.log")
        self.steps()
        patch = mock.patch.dict(
            sys.modules, {f"agent_pm.adapters.{name}": m for name, m in PACKAGES.items()}
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.reset)
        self.configure()

    def steps(self, *steps):
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(list(steps), f)

    def configure(self, runtime="fake", settings=None, **extra):
        """A table per PACKAGES name, each with this test's scenario and log; `extra` keys go into runtime's; settings:
        the config's global keys."""
        tables = {
            name: {
                "implementation": f"adapters.{name}:{name.capitalize()}Adapter",
                "scenario": self.scenario,
                "log": self.log,
            }
            for name in PACKAGES
        }
        tables[runtime].update(extra)
        registry.configure({"adapters": tables, **(settings or {})})

    def spec(self, **fields) -> api.Spec:
        return api.Spec(workdir=self.workdir, **fields)

    def start(
        self, runtime="fake", mode="headless", prompt="hello", spec=None, **kwargs
    ) -> api.Handle:
        handle = api.start(runtime, mode, prompt, spec or self.spec(), **kwargs)
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
        self.assertEqual(self.drain(handle), [api.Exited(0)])
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
                    api.ConfigError,
                    f"mode must be one of headless, tui, in_process, got {bad!r}",
                    lambda: self.start(mode=bad),
                )

    def test_refuses_an_idle_timeout_that_is_no_positive_number(self):
        for bad in (0, -1, 0.0, -0.5, float("nan"), True, False, "1"):
            with self.subTest(bad=bad):
                self.refused(
                    api.ConfigError,
                    f"idle_timeout must be a positive number of seconds, got {bad!r}",
                    lambda: self.start(idle_timeout=bad),
                )

    def test_refuses_a_resume_that_is_no_session_id(self):
        for bad in ("", 1, b"x" * 100):
            with self.subTest(bad=bad):
                self.refused(
                    api.ConfigError,
                    f"resume must be a session id, got {bad!r:.80}",
                    lambda: self.start(resume=bad),
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
                    api.ConfigError,
                    f'sid must be a non-empty printable string without "/", got {shown}',
                    lambda: self.start(sid=bad),
                )

    def test_refuses_a_sid_other_than_resume(self):
        self.refused(
            api.ConfigError,
            "sid 's1' differs from resume 'r1'",
            lambda: self.start(sid="s1", resume="r1"),
        )

    def test_refuses_a_caller_set_spec_sid(self):
        for kwargs in ({}, {"sid": "s1"}, {"resume": "s1"}):
            with self.subTest(kwargs=kwargs):
                self.refused(
                    api.ConfigError,
                    "spec.sid is set by start(): pass sid= or resume=",
                    lambda: self.start(spec=self.spec(sid="s1"), **kwargs),
                )

    def test_a_registry_error_passes_through(self):
        self.refused(
            api.ConfigError,
            f"no [adapters.nope] table in {registry.CONFIG} or {registry.LOCAL}",
            lambda: self.start(runtime="nope"),
        )
        self.configure(model="m")
        self.refused(
            api.ConfigError, "unknown key 'model' in FakeAdapter's config", lambda: self.start()
        )

    def test_tui_on_a_runtime_without_it_is_unsupported(self):
        self.refused(
            api.ConfigError,
            "runtime 'fake' does not support tui mode",
            lambda: self.start(mode="tui"),
        )

    def test_headless_takes_no_name_or_events(self):
        self.refused(api.ConfigError, "name: tui mode only", lambda: self.start(name="w1"))
        self.refused(
            api.ConfigError, "events: tui mode only", lambda: self.start(events=self.scenario)
        )

    def test_refuses_a_launch_without_a_command(self):
        self.refused(
            api.ConfigError, "runtime 'blank' gave no command", lambda: self.start(runtime="blank")
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
                    with self.assertRaises(api.DriverError) as caught:
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
            with self.subTest(message=message), self.assertRaises(api.DriverError) as caught:
                call()
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(self.drain(handle), [api.Exited(0)])

    def test_state_is_live_while_the_process_runs_then_absent(self):
        self.steps({"sleep": 1})
        handle = self.start()
        self.assertEqual(handle.state(), "live")
        self.assertEqual(self.drain(handle), [api.Exited(0)])
        self.assertEqual(handle.state(), "absent")

    def test_attributes_info_reply_and_usage(self):
        cwd = os.path.join(self.dir, "cwd")
        os.mkdir(cwd)
        handle = self.start(spec=self.spec(access=Access(cwd=cwd, project=True)), sid="s1")
        self.assertEqual((handle.sid, handle.runtime, handle.mode), ("s1", "fake", "headless"))
        info = api.SessionInfo(
            "s1", "", cwd, True, os.path.join(self.workdir, "fake-s1.jsonl"), "fake --resume s1"
        )
        self.assertEqual(handle.info(), info)
        self.assertIsNone(handle.reply)
        self.assertIsNone(handle.usage())
        self.assertEqual(self.drain(handle), [api.Exited(0)])
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
        with mock.patch.object(headless, "probe", return_value="7") as probe:
            self.assertEqual(handle.usage(), api.Usage(api.Window(7.0, None), None))
        probe.assert_called_once()
        self.assertEqual(self.drain(handle), [api.Exited(0)])


class InProcessTest(Base):
    def setUp(self):
        super().setUp()
        self.configure(implementation="adapters.fake:InProcessFakeAdapter")

    def run_in_process(self, **kwargs) -> tuple[api.Handle, list]:
        """An in_process session started and drained, each of SPAWNS failing meanwhile and never called."""
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

    def test_records_the_session_its_cwd_access_cwd_else_the_workdir_then_runs_it(self):
        cwd = os.path.join(self.dir, "cwd")
        os.mkdir(cwd)
        for given, project in (("", False), (cwd, True)):
            with self.subTest(cwd=given):
                self.workdir = os.path.join(self.dir, f"work-{project}")
                handle, found = self.run_in_process(
                    spec=self.spec(access=Access(cwd=given, project=project)), sid=SID
                )
                self.assertEqual(found, [api.Exited(0)])
                self.assertEqual(
                    (handle.sid, handle.runtime, handle.mode, handle.name),
                    (SID, "fake", "in_process", None),
                )
                info = api.SessionInfo(SID, "", given or self.workdir, project, "", "")
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
        self.assertEqual(found, [api.Reply("hi"), api.Exited(0)])
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
                api.SessionBound("n1"),
                api.Reply("hi"),
                api.Stderr("warn"),
                api.TurnEnd("done", 0),
                api.Exited(0),
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

    def test_check_gets_the_noted_prompt_the_spec_with_its_sid_and_resume_before_the_record(self):
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
                        "check": "hello\n\nFAKE NOTE",
                        "sid": SID,
                        "resume": resume,
                        "recorded": False,
                    },
                )
                self.assertEqual(
                    (run["prompt"], run["sid"], run["resume"]), (check["check"], SID, resume)
                )

    def test_a_config_error_from_check_refuses_with_no_record_and_no_run(self):
        with mock.patch(
            "threading.Thread.start", side_effect=AssertionError("run started")
        ) as thread:
            self.refused(
                api.ConfigError,
                "tier 4 is unmapped",
                lambda: self.start(runtime="checked", mode="in_process", spec=self.spec(tier=4)),
            )
        thread.assert_not_called()

    def test_takes_no_name_or_events(self):
        self.refused(
            api.ConfigError, "name: tui mode only", lambda: self.start(mode="in_process", name="w1")
        )
        self.refused(
            api.ConfigError,
            "events: tui mode only",
            lambda: self.start(mode="in_process", events=self.scenario),
        )

    def test_the_in_process_fake_supports_no_other_mode(self):
        for mode in ("headless", "tui"):
            with self.subTest(mode=mode):
                self.refused(
                    api.ConfigError,
                    f"runtime 'fake' does not support {mode} mode",
                    lambda: self.start(mode=mode),
                )

    def test_claude_and_codex_do_not_support_it(self):
        registry.configure({"adapters": registry.tables(registry.CONFIG, os.devnull)})
        for runtime in ("claude", "codex"):
            with self.subTest(runtime=runtime):
                self.refused(
                    api.ConfigError,
                    f"runtime {runtime!r} does not support in_process mode",
                    lambda: self.start(runtime=runtime, mode="in_process"),
                )

    def test_a_run_idle_past_idle_timeout_yields_idle_timeout_and_goes_on(self):
        self.steps(
            {"out": {"type": "message", "text": "a"}},
            {"sleep": 0.5},
            {"out": {"type": "message", "text": "b"}},
        )
        _, found = self.run_in_process(idle_timeout=0.1)
        self.assertEqual(found[0], api.Reply("a"))
        self.assertEqual(set(found[1:-2]), {api.IdleTimeout(0.1)})
        self.assertEqual(found[-2:], [api.Reply("b"), api.Exited(0)])

    def test_a_failed_run_start_is_a_driver_error_after_the_session_record(self):
        with mock.patch(
            "threading.Thread.start", side_effect=RuntimeError("can't start new thread")
        ):
            with self.assertRaises(api.DriverError) as caught:
                self.start(mode="in_process")
        self.assertEqual(
            str(caught.exception), "cannot start the in_process run: can't start new thread"
        )
        self.assertEqual([e["kind"] for e in self.written()], ["session"])
        self.assertFalse(os.path.exists(self.log))


class ModeTest(unittest.TestCase):
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
    """A Mode on a Clock: poll(timeout) gives the next script item (at, events, ended) once the clock reaches `at`,
    else ([], False), moving the clock to `at` or on by timeout, whichever is sooner; past LATE s it raises, so a Handle
    that never ends fails. returncode() gives `code`; every call is logged in `calls`."""

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


class HandleOverAModeTest(unittest.TestCase):
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
            "s1",
            "fake",
            "tui",
            host=self.mode,
            workdir=self.dir,
            idle_timeout=idle_timeout,
            info=api.SessionInfo("s1", "", self.dir, False, "", ""),
            **kwargs,
        )

    def events(self, handle) -> list:
        found = list(itertools.islice(handle.events(), 50))
        self.assertLess(len(found), 50, found)
        return found

    def test_events_in_order_the_modes_exited_dropped_exited_last_in_poll_slices(self):
        handle = self.handle(
            (0, [api.Activity("a"), api.Exited(9)], False),
            (1, [api.TurnEnd("done")], False),
            (2, [api.Stderr("e")], True),
            code=3,
        )
        self.assertEqual(
            self.events(handle),
            [api.Activity("a"), api.TurnEnd("done"), api.Stderr("e"), api.Exited(3)],
        )
        self.assertEqual(self.events(handle), [])
        self.assertEqual(handle.reply, "done")
        self.assertEqual({call[1] for call in self.mode.calls if call[0] == "poll"}, {api.POLL})
        self.assertEqual(self.mode.calls[-1], ("returncode", api.POLL))

    def test_each_idle_span_yields_idle_timeout(self):
        handle = self.handle(
            (0, [api.Activity()], False),
            (5, [api.Reply("r")], False),
            (5.5, [], True),
            idle_timeout=2,
        )
        self.assertEqual(
            self.events(handle),
            [api.Activity(), api.IdleTimeout(2), api.IdleTimeout(2), api.Reply("r"), api.Exited(0)],
        )

    def test_after_blocked_no_idle_timeout_until_an_event_but_stderr(self):
        handle = self.handle(
            (0, [api.Blocked("ask")], False),
            (0.5, [api.Stderr("x")], False),
            (3, [api.Activity()], False),
            (4.5, [], True),
            idle_timeout=1,
        )
        self.assertEqual(
            self.events(handle),
            [
                api.Blocked("ask"),
                api.Stderr("x"),
                api.Activity(),
                api.IdleTimeout(1),
                api.Exited(0),
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
        """api.usage("probe"), its probe the Launch of `launch` (argv ("probe-cmd", "-x") unless given) and
        headless.probe patched to give `out`; its result and the patch's mock."""
        self.configure("probe", probe={"argv": ("probe-cmd", "-x"), **launch})
        with mock.patch.object(headless, "probe", return_value=out) as probe:
            return api.usage("probe"), probe

    def test_no_probe_is_none_and_runs_nothing(self):
        with mock.patch.object(headless, "probe", side_effect=AssertionError("probed")) as probe:
            self.assertIsNone(api.usage("fake"))
        probe.assert_not_called()
        self.assertFalse(os.path.exists(self.probe_dir))

    def test_the_probes_stdout_as_the_adapter_parses_it(self):
        found, probe = self.usage("42.5")
        self.assertEqual(found, api.Usage(api.Window(42.5, None), None))
        probe.assert_called_once_with(
            ("probe-cmd", "-x"), cwd=self.probe_dir, env=mock.ANY, timeout=api.PROBE_TIMEOUT
        )

    def test_a_spawn_error_is_none(self):
        self.configure("probe", probe={"argv": ("probe-cmd",)})
        with mock.patch("subprocess.Popen", side_effect=OSError("no exec")) as popen:
            self.assertIsNone(api.usage("probe"))
        popen.assert_called_once()

    def test_a_timeout_is_none(self):
        found, probe = self.usage(None)
        self.assertIsNone(found)
        self.assertEqual(probe.call_args.kwargs["timeout"], api.PROBE_TIMEOUT)

    def test_a_reading_without_windows_is_a_usage(self):
        with mock.patch.object(ProbeAdapter, "usage", return_value=base.Usage(None, None)):
            found, probe = self.usage()
        self.assertEqual(found, api.Usage(None, None))
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
        self.assertEqual(found, api.Usage(api.Window(42.5, None), None))
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
        with self.assertRaises(api.ConfigError):
            api.usage("nope")


class SessionInfoTest(Base):
    def test_the_adapters_session_status(self):
        self.assertEqual(
            api.session_info("probe", "s1", self.dir),
            api.SessionStatus("yes", 1.5, f"probe s1 {self.dir}"),
        )

    def test_unknown_by_default(self):
        self.assertEqual(
            api.session_info("fake", "s1", "/c"), api.SessionStatus("unknown", None, "")
        )

    def test_an_os_error_from_the_adapter_is_unknown(self):
        with mock.patch.object(
            ProbeAdapter, "session_status", side_effect=PermissionError(13, "denied")
        ):
            self.assertEqual(
                api.session_info("probe", "s1", self.dir), api.SessionStatus("unknown", None, "")
            )

    def test_refuses_a_bad_sid_or_cwd_and_passes_registry_errors(self):
        for sid, shown in (("", "''"), ("a/b", "'a/b'"), (5, "5")):
            with self.subTest(sid=sid), self.assertRaises(api.ConfigError) as caught:
                api.session_info("probe", sid, self.dir)
            self.assertEqual(
                str(caught.exception),
                f'sid must be a non-empty printable string without "/", got {shown}',
            )
        for cwd, shown in (("rel", "'rel'"), ("", "''"), ("/c\n", r"'/c\n'"), (None, "None")):
            with self.subTest(cwd=cwd), self.assertRaises(api.ConfigError) as caught:
                api.session_info("probe", "s1", cwd)
            self.assertEqual(
                str(caught.exception), f"cwd must be a printable absolute path, got {shown}"
            )
        with self.assertRaises(api.ConfigError) as caught:
            api.session_info("nope", "s1", self.dir)
        self.assertEqual(
            str(caught.exception),
            f"no [adapters.nope] table in {registry.CONFIG} or {registry.LOCAL}",
        )


def fake_host() -> mock.Mock:
    """tmux_host autospecced, its constants, NAME, TuiError, attach_command and events_file real, PAUSE 0: status None
    (absent), sessions [], option None."""
    host = mock.create_autospec(tmux_host)
    for name in ("RUNNING", "UNKNOWN", "NAME", "OPENER", "SPLITS", "TuiError"):
        setattr(host, name, getattr(tmux_host, name))
    host.PAUSE = 0
    host.attach_command.side_effect = tmux_host.attach_command
    host.events_file.side_effect = tmux_host.events_file
    host.status.return_value = None
    host.sessions.return_value = []
    host.option.return_value = None
    return host


class TuiBase(Base):
    def setUp(self):
        super().setUp()
        self.configure(tui=True)
        self.root = os.path.join(os.path.dirname(self.probe_dir), "tui")
        self.events = os.path.join(self.dir, "events")
        self.host = fake_host()
        for module in (api, tui):
            patch = mock.patch.object(module, "tmux_host", self.host)
            patch.start()
            self.addCleanup(patch.stop)

    def tui(self, **kwargs) -> api.Handle:
        return self.start(mode="tui", **kwargs)

    def nothing_hosted(self):
        self.assertEqual(self.host.mock_calls, [])
        self.assertFalse(os.path.lexists(self.root))

    def name_refused(self, call, bad=(*BAD_NAMES, None)):
        """call(name) refuses each bad name before any tmux call or path use."""
        for name in bad:
            with self.subTest(name=name), self.assertRaises(api.ConfigError) as caught:
                call(name)
            self.assertEqual(
                str(caught.exception), f"name must match [A-Za-z0-9_-]+, got {name!r:.80}"
            )
        self.nothing_hosted()


class TuiStartTest(TuiBase):
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
            [c[0] for c in self.host.mock_calls], ["events_file", "status", "start", "pane"]
        )
        self.assertEqual(
            (handle.name, handle.sid, handle.runtime, handle.mode), ("w1", SID, "fake", "tui")
        )
        self.assertEqual(handle.info(), api.SessionInfo(SID, "", self.workdir, False, "", ""))
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
                "tags": {"@sid": SID},
            },
        )
        self.assertEqual(call.kwargs["env"]["PWD"], self.workdir)
        self.assertEqual(stat.S_IMODE(os.stat(self.events).st_mode), 0o600)
        self.assertEqual(
            {
                k: v
                for k, v in tui.read_state("w1").items()
                if k in ("runtime", "sid", "events", "words")
            },
            {
                "runtime": "fake",
                "sid": SID,
                "events": self.events,
                "words": {"stop": "done", "ask": "blocked", "tool": "working"},
            },
        )

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
        with self.assertRaises(api.DriverError):
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
        self.name_refused(lambda name: self.tui(name=name), BAD_NAMES)
        with self.assertRaises(api.ConfigError) as caught:
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
                self.refused(api.ConfigError, message, lambda: self.tui(name="w1", events=bad))
        self.host.status.assert_not_called()
        self.assertFalse(os.path.lexists(self.root))

    def test_a_running_or_unknown_session_is_refused_with_no_record(self):
        for status, message in (
            (tmux_host.RUNNING, "tui session w1 is running; tmux attach -t '=w1'"),
            (tmux_host.UNKNOWN, "tui session w1: its tmux state is unknown"),
        ):
            with self.subTest(status=status):
                self.host.status.return_value = status
                self.refused(api.DriverError, message, lambda: self.tui(name="w1"))
        self.host.start.assert_not_called()

    def test_a_failed_tmux_start_is_a_driver_error_after_the_record_removing_the_session_dir(self):
        self.host.start.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(api.DriverError) as caught:
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
        self.refused(
            api.ConfigError, "status_line: want true or false", lambda: self.tui(name="w1")
        )
        self.nothing_hosted()

    def test_the_handles_state_screen_send_and_stop_are_the_tui_modes(self):
        handle = self.tui(name="w1")
        self.host.status.return_value = tmux_host.RUNNING
        self.host.read.return_value = "pane text"
        self.assertEqual((handle.state(), handle.screen()), ("live", "pane text"))
        handle.send("hi")
        self.host.paste.assert_called_once_with("w1", "hi")
        self.host.keys.assert_called_once_with("w1", "Enter")
        handle.stop()
        self.host.kill.assert_called_once_with("w1")
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))


class SessionEnvTest(TuiBase):
    def setUp(self):
        super().setUp()
        self.configure("env", probe={"argv": ("probe-cmd",)})
        self.enterContext(mock.patch.dict(os.environ, {"PYTHONPATH": "/parent"}))
        self.access = Access(commands=("git",), env={"PYTHONPATH": IMPORT_ROOT})

    def test_a_headless_session_gets_it_and_its_usage_probe_does_not(self):
        handle, _ = self.run_once(runtime="env", spec=self.spec(access=self.access))
        with open(self.log, encoding="utf-8") as f:
            (entry,) = [json.loads(line) for line in f]
        self.assertEqual(entry["env"]["PYTHONPATH"], IMPORT_ROOT)
        with mock.patch.object(headless, "probe", return_value="7") as probe:
            self.assertEqual(handle.usage(), api.Usage(api.Window(7.0, None), None))
        self.assertEqual(probe.call_args.kwargs["env"]["PYTHONPATH"], "/parent")

    def test_a_tui_session_gets_it(self):
        self.tui(runtime="env", name="w1", spec=self.spec(access=self.access))
        (call,) = self.host.start.call_args_list
        self.assertEqual(call.kwargs["env"]["PYTHONPATH"], IMPORT_ROOT)


class AttachTest(TuiBase):
    def started(self, *hooked) -> None:
        """Tui session w1 started (sid SID), then a hooks.jsonl record per (label, payload) in `hooked`, its @sid SID;
        the host's calls cleared."""
        self.tui(name="w1", sid=SID, events=self.events)
        with open(os.path.join(self.root, "w1", "hooks.jsonl"), "w") as f:
            f.writelines(
                json.dumps({"ts": "", "label": label, "payload": payload}) + "\n"
                for label, payload in hooked
            )
        self.host.option.side_effect = lambda name, key: (
            SID if (name, key) == ("w1", "@sid") else None
        )
        self.host.reset_mock()

    def test_a_handle_of_the_session_with_its_reply_native_id_and_next_records_events(self):
        self.started(("tool", {"session_id": "n1"}), ("stop", {"last_message": "r1"}))
        handle = api.attach("w1", idle_timeout=2)
        self.assertEqual(
            (handle.name, handle.sid, handle.runtime, handle.mode, handle.reply),
            ("w1", SID, "fake", "tui", "r1"),
        )
        self.assertEqual(handle.info(), api.SessionInfo(SID, "n1", self.workdir, False, "", ""))
        self.assertEqual(
            self.host.mock_calls, [mock.call.option("w1", "@sid"), mock.call.pane("w1")]
        )
        with open(os.path.join(self.root, "w1", "hooks.jsonl"), "a") as f:
            f.write(
                json.dumps({"ts": "", "label": "stop", "payload": {"last_message": "r2"}}) + "\n"
            )
        self.assertEqual(next(handle.events()), api.TurnEnd("r2", 0))
        self.assertEqual(handle.reply, "r2")

    def test_a_bad_name_or_idle_timeout_is_refused_before_tmux_or_a_path(self):
        self.name_refused(api.attach)
        for bad in (0, -1, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(api.ConfigError) as caught:
                api.attach("w1", idle_timeout=bad)
            self.assertEqual(
                str(caught.exception),
                f"idle_timeout must be a positive number of seconds, got {bad!r}",
            )
        self.nothing_hosted()

    def test_no_session_dir_or_another_sid_is_no_tui_session(self):
        with self.assertRaises(api.DriverError) as caught:
            api.attach("w1")
        self.assertTrue(str(caught.exception).startswith("no tui session w1: "), caught.exception)
        self.nothing_hosted()
        self.started()
        for found in ("other", None):
            with self.subTest(found=found), self.assertRaises(api.DriverError) as caught:
                self.host.option.side_effect = lambda name, key: found
                api.attach("w1")
            self.assertEqual(str(caught.exception), "no tui session w1")

    def test_a_runtime_without_tui_or_a_registry_error_is_a_driver_error(self):
        self.started()
        self.configure(tui=False)
        for message in (
            "no tui session w1: runtime 'fake' has no tui mode",
            f"no tui session w1: no [adapters.fake] table in {registry.CONFIG} or {registry.LOCAL}",
        ):
            with self.subTest(message=message), self.assertRaises(api.DriverError) as caught:
                api.attach("w1")
            self.assertEqual(str(caught.exception), message)
            registry.configure({"adapters": {}})
        self.host.option.assert_not_called()


class SessionsTest(TuiBase):
    def test_the_sessions_with_the_prefix_from_tmux(self):
        self.host.sessions.return_value = ["w1", "w2"]
        self.assertEqual(api.sessions("w"), api.Sessions("w", ("w1", "w2")))
        self.assertEqual(api.sessions(), api.Sessions("", ("w1", "w2")))
        self.assertEqual(self.host.mock_calls, [mock.call.sessions("w"), mock.call.sessions("")])

    def test_no_server_is_no_names_and_a_tmux_failure_unknown(self):
        self.assertEqual(api.sessions("w").state("w1"), "absent")
        self.host.sessions.return_value = None
        self.assertEqual(api.sessions("w"), api.Sessions("w", None))
        self.assertEqual(api.sessions("w").state("w1"), "unknown")

    def test_a_bad_prefix_is_refused(self):
        for bad in ("a b", "../", "w\n", None, 5):
            with self.subTest(bad=bad), self.assertRaises(api.ConfigError) as caught:
                api.sessions(bad)
            self.assertEqual(
                str(caught.exception), f"prefix must match [A-Za-z0-9_-]*, got {bad!r:.80}"
            )
        self.nothing_hosted()

    def test_state_of_a_name_with_the_prefix_and_no_collection_protocol(self):
        found = api.Sessions("w", ("w1", "w2"))
        self.assertEqual(
            [found.state(n) for n in ("w1", "w2", "w3", "w")], ["live", "live", "absent", "absent"]
        )
        for name in ("x1", "", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                found.state(name)
        for dunder in ("__iter__", "__len__", "__bool__", "__contains__"):
            self.assertFalse(hasattr(api.Sessions, dunder), dunder)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            found.names = ()


class CloseTest(TuiBase):
    def test_kills_the_session_and_removes_its_dir(self):
        self.tui(name="w1")
        self.host.reset_mock()
        api.close("w1")
        self.assertEqual(self.host.mock_calls, [mock.call.kill("w1")])
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))
        self.assertTrue(os.path.isdir(self.root))

    def test_a_bad_name_is_refused_before_tmux_or_a_path(self):
        self.name_refused(api.close)


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
        self.assertIsNone(api.show("w1", split="below", split_from="m1", opener="w0t0p0:AB-12"))
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
            self.assertEqual(api.show("w1"), "docker exec -it box tmux attach -t '=w1'")
        self.assertEqual(
            logs.output, [f"WARNING:driver:show w1: {NO_PANE}; watch it with tmux attach -t '=w1'"]
        )
        self.host.open_pane.assert_called_once_with(
            "w1", split=None, split_from=None, opener=None, per_column=None, retile=None
        )

    def test_an_absent_session_is_a_driver_error(self):
        self.host.status.return_value = None
        with self.assertRaises(api.DriverError) as caught:
            api.show("w1")
        self.assertEqual(str(caught.exception), "no tmux session w1")
        self.host.open_pane.assert_not_called()

    def test_a_bad_name_placement_opener_or_setting_is_refused_before_tmux(self):
        self.name_refused(api.show)
        for kwargs, message in (*PLACEMENTS, *(({"opener": o}, m) for o, m in OPENERS)):
            with self.subTest(kwargs=kwargs), self.assertRaises(api.ConfigError) as caught:
                api.show("w1", **kwargs)
            self.assertEqual(str(caught.exception), message)
        self.configure(tui=True, settings={"grid_retile": "never"})
        with self.assertRaises(api.ConfigError) as caught:
            api.show("w1")
        self.assertEqual(str(caught.exception), "grid_retile: want one of all, open-close, off")
        self.nothing_hosted()


class LayoutTest(TuiBase):
    def test_the_callers_opener_once_its_pane_is_found(self):
        self.host.opener.return_value = "mgr"
        self.assertEqual(api.layout(), "mgr")
        self.assertEqual(self.host.mock_calls, [mock.call.anchor(None), mock.call.opener()])

    def test_beside_a_live_split_from_the_opener_may_be_none(self):
        self.host.status.return_value = tmux_host.RUNNING
        self.host.opener.side_effect = tmux_host.TuiError(NO_PANE)
        self.assertIsNone(api.layout("below", "m1"))
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
                with self.assertRaises(api.DriverError) as caught:
                    api.layout(split_from=split_from)
                self.assertEqual(str(caught.exception), f"no pane to show the TUI beside {message}")

    def test_a_bad_split_or_split_from_or_an_absent_one_is_refused(self):
        for kwargs, message in PLACEMENTS:
            with self.subTest(kwargs=kwargs), self.assertRaises(api.ConfigError) as caught:
                api.layout(**kwargs)
            self.assertEqual(str(caught.exception), message)
        self.nothing_hosted()
        with self.assertRaises(api.ConfigError) as caught:
            api.layout(split_from="m1")
        self.assertEqual(str(caught.exception), "no tmux session m1")
        self.assertEqual(self.host.mock_calls, [mock.call.status("m1")])


class AttachCommandTest(TuiBase):
    def test_tmux_hosts_command_after_the_name_check(self):
        self.assertEqual(api.attach_command("w1"), "tmux attach -t '=w1'")
        with mock.patch.dict(os.environ, {"TUI_ATTACH_PREFIX": "docker exec -it box"}):
            self.assertEqual(api.attach_command("w1"), "docker exec -it box tmux attach -t '=w1'")
        self.host.reset_mock()
        self.name_refused(api.attach_command)


class DetachTest(TuiBase):
    def test_hosts_argv_with_the_signals_blocked_and_the_opener(self):
        env = {"PATH": "/bin", "TMUX": "x"}
        self.assertIsNone(api.detach("d1", ["tool", "a b"], self.dir, env, opener="mgr"))
        self.assertEqual(
            self.host.mock_calls,
            [
                mock.call.detach(
                    "d1", ["tool", "a b"], cwd=self.dir, env=env, block=api.SIGNALS, opener="mgr"
                )
            ],
        )
        api.detach("d2", ("tool",), self.dir, env)
        self.assertEqual(self.host.detach.call_args.kwargs["opener"], None)

    def test_a_bad_name_argv_cwd_or_opener_is_refused_before_tmux(self):
        self.name_refused(lambda name: api.detach(name, ["tool"], self.dir, {}))
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
                self.assertRaises(api.ConfigError) as caught,
            ):
                api.detach("d1", *args, {}, **kwargs)
            self.assertEqual(str(caught.exception), message)
        self.nothing_hosted()

    def test_a_tmux_failure_is_a_driver_error(self):
        self.host.detach.side_effect = tmux_host.TuiError("session d1 exists")
        with self.assertRaises(api.DriverError) as caught:
            api.detach("d1", ["tool"], self.dir, {})
        self.assertEqual(str(caught.exception), "detach d1: session d1 exists")


class ExportsTest(unittest.TestCase):
    def test_reexports_and_constants(self):
        for name in (
            "Access",
            "Spec",
            "SessionInfo",
            "ConfigError",
            "SessionBound",
            "Activity",
            "Reply",
            "TurnEnd",
            "Blocked",
            "LimitHit",
            "Exited",
            "Event",
            "Stderr",
            "Usage",
            "Window",
            "SessionStatus",
            "RESUMABLE",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(api, name), getattr(base, name))
        self.assertIs(api.records, records)
        self.assertIs(api.session, records.session)
        self.assertIs(api.DriverError, errors.DriverError)
        for name in (
            "LimitHit",
            "Usage",
            "Window",
            "SessionStatus",
            "RESUMABLE",
            "usage",
            "session_info",
            "session",
            "DriverError",
            "Mode",
            "Sessions",
            "attach",
            "close",
            "sessions",
            "SIGNALS",
            "attach_command",
            "detach",
            "layout",
            "show",
        ):
            with self.subTest(name=name):
                self.assertIn(name, api.__all__)
        self.assertEqual([name for name in api.__all__ if not hasattr(api, name)], [])
        self.assertFalse(hasattr(api, "NO_TUI"))
        self.assertEqual(
            (api.MODES, api.STATES, api.POLL),
            (("headless", "tui", "in_process"), ("live", "absent", "unknown"), 0.5),
        )
        self.assertEqual((api.PROBE_DIR, api.PROBE_TIMEOUT), ("~/.agent-pm/probe", 120))
        self.assertEqual(api.IdleTimeout(0.3).seconds, 0.3)
        self.assertEqual(api.SIGNALS, (signal.SIGHUP, signal.SIGTERM, signal.SIGINT))


if __name__ == "__main__":
    unittest.main()
