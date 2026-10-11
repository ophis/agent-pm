"""new/core/src/driver/api.py: start()'s checks, its session record, sid and Runtime note, a headless Handle's surface,
usage() and session_info(), over the fake adapter and test adapters registered in-process (sys.modules) with
[adapters.<name>] tables given to registry.configure. Wherever nothing may start, subprocess.Popen is patched to fail;
usage()'s probe is headless.probe patched, but for a spawn error. The Mode protocol, and a Handle over a scripted
Mode on a fake clock (time.monotonic patched)."""
import inspect
import itertools
import json
import os
import stat
import sys
import tempfile
import threading
import types
import unittest
import uuid
from unittest import mock

from new.tests import fake_adapter, hermetic
from adapters import base
from adapters.base import Access, Launch
from driver import api, errors, records, registry
from driver.modes import headless

WAIT = 10   # upper bound on any wait, in seconds


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


def package(name, cls) -> types.ModuleType:
    """An in-memory module adapters.<name> holding `cls`."""
    module = types.ModuleType(f"adapters.{name}")
    setattr(module, cls.__name__, cls)
    return module


PACKAGES = {"fake": fake_adapter, **{name: package(name, cls) for name, cls in
                                     (("blank", BlankAdapter), ("failing", FailingAdapter), ("quiet", QuietAdapter),
                                      ("probe", ProbeAdapter))}}


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
        patch = mock.patch.dict(sys.modules, {f"adapters.{name}": m for name, m in PACKAGES.items()})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.reset)
        self.configure()

    def steps(self, *steps):
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(list(steps), f)

    def configure(self, runtime="fake", **extra):
        """A table per PACKAGES name, each with this test's scenario and log; `extra` keys go into runtime's."""
        tables = {name: {"implementation": f"adapters.{name}:{name.capitalize()}Adapter", "scenario": self.scenario,
                         "log": self.log} for name in PACKAGES}
        tables[runtime].update(extra)
        registry.configure({"adapters": tables})

    def spec(self, **fields) -> api.Spec:
        return api.Spec(workdir=self.workdir, **fields)

    def start(self, runtime="fake", mode="headless", prompt="hello", spec=None, **kwargs) -> api.Handle:
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
                self.refused(api.ConfigError, f"mode must be headless or tui, got {bad!r}",
                             lambda: self.start(mode=bad))

    def test_refuses_an_idle_timeout_that_is_no_positive_number(self):
        for bad in (0, -1, 0.0, -0.5, float("nan"), True, False, "1"):
            with self.subTest(bad=bad):
                self.refused(api.ConfigError, f"idle_timeout must be a positive number of seconds, got {bad!r}",
                             lambda: self.start(idle_timeout=bad))

    def test_refuses_a_resume_that_is_no_session_id(self):
        for bad in ("", 1, b"x" * 100):
            with self.subTest(bad=bad):
                self.refused(api.ConfigError, f"resume must be a session id, got {bad!r:.80}",
                             lambda: self.start(resume=bad))

    def test_refuses_a_sid_that_is_not_a_non_empty_printable_string_without_a_slash(self):
        for bad, shown in (("", "''"), ("a/b", "'a/b'"), ("a\tb", r"'a\tb'"), (5, "5"),
                           ("s" * 200 + "\n", "'" + "s" * 79)):
            with self.subTest(bad=bad):
                self.refused(api.ConfigError, f'sid must be a non-empty printable string without "/", got {shown}',
                             lambda: self.start(sid=bad))

    def test_refuses_a_sid_other_than_resume(self):
        self.refused(api.ConfigError, "sid 's1' differs from resume 'r1'", lambda: self.start(sid="s1", resume="r1"))

    def test_refuses_a_caller_set_spec_sid(self):
        for kwargs in ({}, {"sid": "s1"}, {"resume": "s1"}):
            with self.subTest(kwargs=kwargs):
                self.refused(api.ConfigError, "spec.sid is set by start(): pass sid= or resume=",
                             lambda: self.start(spec=self.spec(sid="s1"), **kwargs))

    def test_a_registry_error_passes_through(self):
        self.refused(api.ConfigError, f"no [adapters.nope] table in {registry.CONFIG} or {registry.LOCAL}",
                     lambda: self.start(runtime="nope"))
        self.configure(model="m")
        self.refused(api.ConfigError, "unknown key 'model' in FakeAdapter's config", lambda: self.start())

    def test_tui_on_a_runtime_without_it_is_unsupported(self):
        self.refused(api.ConfigError, "runtime 'fake' does not support tui mode", lambda: self.start(mode="tui"))

    def test_tui_on_a_runtime_with_it_is_not_available_yet(self):
        self.configure(tui=True)
        self.refused(api.ConfigError, "tui mode is not available yet", lambda: self.start(mode="tui"))
        self.refused(api.ConfigError, "tui mode is not available yet", lambda: self.start(mode="tui", name="w1"))

    def test_headless_takes_no_name(self):
        self.refused(api.ConfigError, "name: tui mode only", lambda: self.start(name="w1"))

    def test_refuses_a_launch_without_a_command(self):
        self.refused(api.ConfigError, "runtime 'blank' gave no command", lambda: self.start(runtime="blank"))

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
        for call, message in ((lambda: handle.send("x"), "send: tui mode only"),
                              (handle.screen, "screen: tui mode only")):
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
        info = api.SessionInfo("s1", "", cwd, True, os.path.join(self.workdir, "fake-s1.jsonl"), "fake --resume s1")
        self.assertEqual(handle.info(), info)
        self.assertIsNone(handle.reply)
        self.assertIsNone(handle.usage())
        self.assertEqual(self.drain(handle), [api.Exited(0)])
        (session,) = self.written()
        self.assertEqual({k: v for k, v in session.items() if k != "ts"},
                         {"kind": "session", "sid": "s1", "cwd": cwd, "project": True,
                          "transcript": info.transcript, "resume": info.resume})

    def test_usage_is_its_runtimes(self):
        self.configure("probe", probe={"argv": ("probe-cmd",)})
        handle = self.start(runtime="probe")
        with mock.patch("driver.modes.headless.probe", return_value="7") as probe:
            self.assertEqual(handle.usage(), api.Usage(api.Window(7.0, None), None))
        probe.assert_called_once()
        self.assertEqual(self.drain(handle), [api.Exited(0)])

    def test_attach_is_not_available_yet(self):
        with self.assertRaises(api.ConfigError) as caught:
            api.attach("w1")
        self.assertEqual(str(caught.exception), "tui mode is not available yet")


class ModeTest(unittest.TestCase):
    def test_the_protocols_methods_and_headless_mode_has_each_with_its_parameters(self):
        def params(function):
            return [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()]
        members = sorted(n for n, v in vars(api.Mode).items() if callable(v) and not n.startswith("_"))
        self.assertEqual(members, ["poll", "returncode", "screen", "send", "state", "stop"])
        for name in members:
            with self.subTest(name=name):
                self.assertEqual(params(getattr(headless.HeadlessMode, name)), params(getattr(api.Mode, name)))


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
        return api.Handle("s1", "fake", "tui", host=self.mode, workdir=self.dir, idle_timeout=idle_timeout,
                          info=api.SessionInfo("s1", "", self.dir, False, "", ""), **kwargs)

    def events(self, handle) -> list:
        found = list(itertools.islice(handle.events(), 50))
        self.assertLess(len(found), 50, found)
        return found

    def test_events_in_order_the_modes_exited_dropped_exited_last_in_poll_slices(self):
        handle = self.handle((0, [api.Activity("a"), api.Exited(9)], False), (1, [api.TurnEnd("done")], False),
                             (2, [api.Stderr("e")], True), code=3)
        self.assertEqual(self.events(handle), [api.Activity("a"), api.TurnEnd("done"), api.Stderr("e"), api.Exited(3)])
        self.assertEqual(self.events(handle), [])
        self.assertEqual(handle.reply, "done")
        self.assertEqual({call[1] for call in self.mode.calls if call[0] == "poll"}, {api.POLL})
        self.assertEqual(self.mode.calls[-1], ("returncode", api.POLL))

    def test_each_idle_span_yields_idle_timeout(self):
        handle = self.handle((0, [api.Activity()], False), (5, [api.Reply("r")], False), (5.5, [], True),
                             idle_timeout=2)
        self.assertEqual(self.events(handle), [api.Activity(), api.IdleTimeout(2), api.IdleTimeout(2), api.Reply("r"),
                                               api.Exited(0)])

    def test_after_blocked_no_idle_timeout_until_an_event_but_stderr(self):
        handle = self.handle((0, [api.Blocked("ask")], False), (0.5, [api.Stderr("x")], False),
                             (3, [api.Activity()], False), (4.5, [], True), idle_timeout=1)
        self.assertEqual(self.events(handle), [api.Blocked("ask"), api.Stderr("x"), api.Activity(), api.IdleTimeout(1),
                                               api.Exited(0)])

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
        self.assertEqual((handle.state(), handle.screen(), handle.screen(5)), ("unknown", "pane", "pane"))
        handle.send("hi")
        handle.stop()
        self.assertEqual(self.mode.calls, [("state",), ("screen", None), ("screen", 5), ("send", "hi"), ("stop",)])
        self.assertEqual(self.handle(name="w1").name, "w1")


class UsageTest(Base):
    def usage(self, out="42.5", **launch):
        """api.usage("probe"), its probe the Launch of `launch` (argv ("probe-cmd", "-x") unless given) and
        headless.probe patched to give `out`; its result and the patch's mock."""
        self.configure("probe", probe={"argv": ("probe-cmd", "-x"), **launch})
        with mock.patch("driver.modes.headless.probe", return_value=out) as probe:
            return api.usage("probe"), probe

    def test_no_probe_is_none_and_runs_nothing(self):
        with mock.patch("driver.modes.headless.probe", side_effect=AssertionError("probed")) as probe:
            self.assertIsNone(api.usage("fake"))
        probe.assert_not_called()
        self.assertFalse(os.path.exists(self.probe_dir))

    def test_the_probes_stdout_as_the_adapter_parses_it(self):
        found, probe = self.usage("42.5")
        self.assertEqual(found, api.Usage(api.Window(42.5, None), None))
        probe.assert_called_once_with(("probe-cmd", "-x"), cwd=self.probe_dir, env=mock.ANY, timeout=api.PROBE_TIMEOUT)

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
        for kind, make in (("symlink", lambda: os.symlink(target, self.probe_dir)),
                           ("file", lambda: open(self.probe_dir, "w").close())):
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
        self.assertEqual((env.get("API_DROP"), env["API_KEEP"], env["API_ADD"], env["PWD"]), (None, "k", "a", cwd))
        self.assertEqual(probe.call_args.kwargs["cwd"], cwd)
        self.assertFalse(os.path.exists(self.probe_dir))

    def test_a_registry_error_raises(self):
        with self.assertRaises(api.ConfigError):
            api.usage("nope")


class SessionInfoTest(Base):
    def test_the_adapters_session_status(self):
        self.assertEqual(api.session_info("probe", "s1", self.dir),
                         api.SessionStatus("yes", 1.5, f"probe s1 {self.dir}"))

    def test_unknown_by_default(self):
        self.assertEqual(api.session_info("fake", "s1", "/c"), api.SessionStatus("unknown", None, ""))

    def test_an_os_error_from_the_adapter_is_unknown(self):
        with mock.patch.object(ProbeAdapter, "session_status", side_effect=PermissionError(13, "denied")):
            self.assertEqual(api.session_info("probe", "s1", self.dir), api.SessionStatus("unknown", None, ""))

    def test_refuses_a_bad_sid_or_cwd_and_passes_registry_errors(self):
        for sid, shown in (("", "''"), ("a/b", "'a/b'"), (5, "5")):
            with self.subTest(sid=sid), self.assertRaises(api.ConfigError) as caught:
                api.session_info("probe", sid, self.dir)
            self.assertEqual(str(caught.exception),
                             f'sid must be a non-empty printable string without "/", got {shown}')
        for cwd, shown in (("rel", "'rel'"), ("", "''"), ("/c\n", r"'/c\n'"), (None, "None")):
            with self.subTest(cwd=cwd), self.assertRaises(api.ConfigError) as caught:
                api.session_info("probe", "s1", cwd)
            self.assertEqual(str(caught.exception), f"cwd must be a printable absolute path, got {shown}")
        with self.assertRaises(api.ConfigError) as caught:
            api.session_info("nope", "s1", self.dir)
        self.assertEqual(str(caught.exception), f"no [adapters.nope] table in {registry.CONFIG} or {registry.LOCAL}")


class ExportsTest(unittest.TestCase):
    def test_reexports_and_constants(self):
        for name in ("Access", "Spec", "SessionInfo", "ConfigError", "SessionBound", "Activity", "Reply", "TurnEnd",
                     "Blocked", "LimitHit", "Exited", "Event", "Stderr", "Usage", "Window", "SessionStatus",
                     "RESUMABLE"):
            with self.subTest(name=name):
                self.assertIs(getattr(api, name), getattr(base, name))
        self.assertIs(api.records, records)
        self.assertIs(api.session, records.session)
        self.assertIs(api.DriverError, errors.DriverError)
        for name in ("LimitHit", "Usage", "Window", "SessionStatus", "RESUMABLE", "usage", "session_info", "session",
                     "DriverError", "Mode"):
            with self.subTest(name=name):
                self.assertIn(name, api.__all__)
        self.assertEqual([name for name in api.__all__ if not hasattr(api, name)], [])
        self.assertEqual((api.MODES, api.STATES, api.POLL), (("headless", "tui"), ("live", "absent", "unknown"), 0.5))
        self.assertEqual((api.PROBE_DIR, api.PROBE_TIMEOUT), ("~/.agent-pm/probe", 120))
        self.assertEqual(api.IdleTimeout(0.3).seconds, 0.3)


if __name__ == "__main__":
    unittest.main()
