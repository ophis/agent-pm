"""Driver handle behavior with caller-owned dependencies."""

import dataclasses
import itertools
import logging
import os
import stat
import tempfile
from unittest import mock

from agent_pm.session.engine import api, records
from agent_pm.session.engine import types as engine_types
from agent_pm.session.engine.lifecycle.types import POLL, HandleServices, HandleState
from agent_pm.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ConfigError,
    Exited,
    IdleTimeout,
    Reply,
    SessionBound,
    SessionInfo,
    SessionRequest,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
    Window,
)

from session.tests import fake_adapter
from session.tests.case import Case
from session.tests.unit.driver.support import (
    Base,
    Clock,
    ProbeAdapter,
    StubMode,
)


class HandleOverAModeTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.clock = Clock()
        patch = mock.patch("time.monotonic", lambda: self.clock.now)
        patch.start()
        self.addCleanup(patch.stop)

    def handle(self, *script, code=0, idle_timeout=None, native_id="", **kwargs) -> api.Handle:
        self.mode = StubMode(self.clock, script, code)
        return api.Handle(
            "fake",
            "tui",
            Spec(workdir=self.dir, sid="s1", name=kwargs.pop("name", None)),
            state=HandleState(
                info=SessionInfo("s1", native_id, self.dir, False, "", ""),
                idle_timeout=idle_timeout,
            ),
            services=HandleServices(host=self.mode, **kwargs),
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

    def test_metadata_failure_is_diagnostic_and_remains_best_effort_with_logging_disabled(self):
        adapter = fake_adapter.FakeAdapter({})
        adapter.session_metadata = mock.Mock(side_effect=RuntimeError("metadata unavailable"))
        handle = self.handle(adapter=adapter, native_id="n1")
        with self.assertLogs(level="DEBUG") as logs:
            previous = handle.info()
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].name, "driver")
        self.assertIsInstance(logs.records[0].exc_info[1], RuntimeError)
        disabled = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            self.assertEqual(handle.info(), previous)
        finally:
            logging.disable(disabled)

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
        self.assertEqual({call[1] for call in self.mode.calls if call[0] == "poll"}, {POLL})
        self.assertEqual(self.mode.calls[-1], ("returncode", POLL))

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

    def test_usage_parse_failure_keeps_its_fallback_with_logging_disabled(self):
        disabled = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            self.assertIsNone(self.usage("garbage")[0])
        finally:
            logging.disable(disabled)

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
