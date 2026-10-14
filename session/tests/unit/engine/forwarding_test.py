"""The forwarding wrappers kept until 356-14: Driver (today's constructor, from_config, registry and
method signatures, each method forwarding to its configured HostedSession class, session_cls),
Handle, sessions, close, note and session_record, and the session package exporting them.
"""

import dataclasses
import inspect
import itertools
import json
import os
import signal
import tempfile
from pathlib import Path
from unittest import mock

import agent_pm.session as session_pkg
from agent_pm.session import Driver, Session
from agent_pm.session.engine import forwarding, hosted
from agent_pm.session.engine import types as engine_types
from agent_pm.session.engine.forwarding import (
    Handle,
    close,
    note,
    session_record,
    sessions,
)
from agent_pm.session.engine.hosted import HostedSession
from agent_pm.session.engine.records import RunLog
from agent_pm.session.engine.types import (
    ConfigError,
    DriverError,
    Exited,
    Hosts,
    IdleTimeout,
    SessionStatus,
    Usage,
    Window,
)
from agent_pm.session.hosts.base import SIGNALS
from agent_pm.session.hosts.tmux import TmuxHost, tmux_host
from agent_pm.session.modes import tui
from agent_pm.utils import ProcessControl, probe

from session.tests.case import HOSTS, Case
from session.tests.hosted_support import (
    NO_PANE,
    OPENERS,
    PLACEMENTS,
    SID,
    Base,
    ProbeAdapter,
    TuiBase,
    drive,
    fake_tmux,
    listed,
)

POSITIONAL, KEYWORD = inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY
EMPTY = inspect.Parameter.empty
SIGNATURES = {
    "__init__": [
        ("self", POSITIONAL, EMPTY),
        ("adapters", KEYWORD, EMPTY),
        ("hosts", KEYWORD, EMPTY),
        ("default_host", KEYWORD, None),
        ("host", KEYWORD, tmux_host),
        ("probe", KEYWORD, probe),
        ("clock", KEYWORD, None),
        ("class_loader", KEYWORD, forwarding.load_class),
        ("harness_host", KEYWORD, None),
    ],
    "from_config": [
        ("config", POSITIONAL, EMPTY),
        ("hosts", KEYWORD, None),
        ("host", KEYWORD, tmux_host),
        ("probe", KEYWORD, probe),
        ("clock", KEYWORD, None),
        ("class_loader", KEYWORD, forwarding.load_class),
        ("harness_host", KEYWORD, None),
    ],
    "start": [
        ("self", POSITIONAL, EMPTY),
        ("runtime", POSITIONAL, EMPTY),
        ("mode", POSITIONAL, EMPTY),
        ("prompt", POSITIONAL, EMPTY),
        ("spec", POSITIONAL, EMPTY),
        ("resume", KEYWORD, None),
        ("events", KEYWORD, None),
        ("idle_timeout", KEYWORD, None),
    ],
    "attach": [
        ("self", POSITIONAL, EMPTY),
        ("name", POSITIONAL, EMPTY),
        ("idle_timeout", POSITIONAL, None),
    ],
    "show": [
        ("self", POSITIONAL, EMPTY),
        ("name", POSITIONAL, EMPTY),
        ("split", POSITIONAL, None),
        ("split_from", POSITIONAL, None),
        ("opener", POSITIONAL, None),
    ],
    "usage": [("self", POSITIONAL, EMPTY), ("runtime", POSITIONAL, EMPTY)],
    "session_record": [("workdir", POSITIONAL, EMPTY), ("sid", POSITIONAL, EMPTY)],
    "session_status": [
        ("self", POSITIONAL, EMPTY),
        ("runtime", POSITIONAL, EMPTY),
        ("sid", POSITIONAL, EMPTY),
        ("cwd", POSITIONAL, EMPTY),
        ("workdir", KEYWORD, EMPTY),
    ],
    "writable_dirs": [
        ("self", POSITIONAL, EMPTY),
        ("runtime", POSITIONAL, EMPTY),
        ("spec", POSITIONAL, EMPTY),
        ("resume", POSITIONAL, None),
    ],
    "sessions": [("self", POSITIONAL, EMPTY), ("match", POSITIONAL, None)],
    "close": [("self", POSITIONAL, EMPTY), ("name", POSITIONAL, EMPTY)],
    "layout": [
        ("self", POSITIONAL, EMPTY),
        ("split", POSITIONAL, None),
        ("split_from", POSITIONAL, None),
    ],
    "attach_command": [("self", POSITIONAL, EMPTY), ("name", POSITIONAL, EMPTY)],
    "detach": [
        ("self", POSITIONAL, EMPTY),
        ("name", POSITIONAL, EMPTY),
        ("argv", POSITIONAL, EMPTY),
        ("cwd", POSITIONAL, EMPTY),
        ("env", POSITIONAL, EMPTY),
        ("opener", KEYWORD, None),
    ],
}


def outcome(call):
    """call()'s result, else its exception's type and text."""
    try:
        return "returned", call()
    except (ConfigError, DriverError) as e:
        return type(e), str(e)


def clock():
    return 0.0


class ExportsTest(Case):
    def test_the_session_package_exports_them_not_the_module(self):
        for name in ("Driver", "Handle", "note", "session_record", "sessions", "close"):
            with self.subTest(name=name):
                self.assertIs(getattr(session_pkg, name), getattr(forwarding, name))
                self.assertIn(name, session_pkg.__all__)
        self.assertIs(session_pkg.SIGNALS, SIGNALS)
        self.assertIs(Handle, Session)
        for name in ("api", "forwarding", "hosted", "HostedSession", "TmuxHost", "holds"):
            with self.subTest(name=name):
                self.assertNotIn(name, session_pkg.__all__)
        self.assertFalse(hasattr(forwarding, "holds"))
        self.assertIs(session_pkg.RunLog, RunLog)
        for name in session_pkg.__all__:
            if hasattr(engine_types, name):
                with self.subTest(name=name):
                    self.assertIs(getattr(session_pkg, name), getattr(engine_types, name))

    def test_constants(self):
        self.assertFalse(hasattr(forwarding, "__all__"))
        self.assertEqual(SIGNALS, (signal.SIGHUP, signal.SIGTERM, signal.SIGINT))


class WrappersTest(TuiBase):
    def test_each_forwards_to_its_target(self):
        self.configure("probe", probe={"argv": ("probe-cmd",)})
        rows = [listed("w1", display="mgr"), listed("w2")]
        self.host.listing.return_value = rows
        spec = self.spec()
        with tempfile.TemporaryDirectory() as d:
            log = RunLog(d)
            log.note("session", sid=SID, cwd=d, project=True, transcript="", resume="")
            pairs = (
                (lambda: self.driver.sessions("w.*"), lambda: self.session.list("w.*")),
                (lambda: self.driver.usage("fake"), lambda: self.session.usage("fake")),
                (
                    lambda: self.driver.writable_dirs("fake", spec),
                    lambda: self.session.writable_dirs(dataclasses.replace(spec, runtime="fake")),
                ),
                (
                    lambda: self.driver.session_status("probe", SID, d, workdir=d),
                    lambda: SessionStatus("yes", 1.5, f"probe {SID} {d}"),
                ),
                (lambda: session_record(d, SID), lambda: log.session(SID).persisted()),
                (
                    lambda: Driver.session_record(d, SID),
                    lambda: log.session(SID).persisted(),
                ),
                (lambda: sessions("("), lambda: HostedSession.list("(")),
                (lambda: close("a b"), lambda: tui.TuiMode.close("a b", host=self.session.host())),
                (
                    lambda: self.driver.close("a b"),
                    lambda: tui.TuiMode.close("a b", host=self.session.host()),
                ),
            )
            for index, (wrapper, target) in enumerate(pairs):
                with self.subTest(index=index):
                    self.assertEqual(outcome(wrapper), outcome(target))
            note(d, "x", a=1)
            self.assertEqual([(e["kind"], e.get("a")) for e in log.read()][-1], ("x", 1))
            self.assertIsNone(session_record(d, "other"))


class DriverConfigurationTest(Case):
    def test_its_signatures_are_todays(self):
        for name, want in SIGNATURES.items():
            with self.subTest(name=name):
                found = inspect.signature(getattr(Driver, name)).parameters.values()
                self.assertEqual([(p.name, p.kind, p.default) for p in found], want)

    def test_its_registry_is_the_setup_registry_of_its_session_class(self):
        adapters = {"a": {"implementation": "agent_pm.session.adapters.a:AAdapter"}}
        driver = Driver(adapters=adapters, hosts=HOSTS, default_host="tmux")
        self.assertTrue(issubclass(driver.session_cls, HostedSession))
        self.assertIsNot(driver.session_cls, HostedSession)
        self.assertIs(driver.registry, driver.session_cls._config().registry)
        self.assertEqual(driver.registry.configured(), ["a"])
        other = Driver(adapters={}, hosts=HOSTS, default_host="tmux")
        self.assertIsNot(other.session_cls, driver.session_cls)
        self.assertIsNone(HostedSession._setup)

    def test_host_is_the_tmux_module_of_its_tmux_host_probe_and_clock_its_setups(self):
        tmux, probe = fake_tmux(), mock.Mock()
        driver = Driver(
            adapters={}, hosts=HOSTS, default_host="tmux", host=tmux, probe=probe, clock=clock
        )
        host = driver.session_cls.host()
        self.assertIsInstance(host, TmuxHost)
        self.assertIs(host.tmux, tmux)
        self.assertEqual((driver.host, driver.probe, driver.clock), (tmux, probe, clock))
        setup = driver.session_cls._config()
        self.assertEqual((setup.probe, setup.clock), (probe, clock))

    def test_configure_is_concrete_and_refuses_bad_input(self):
        with self.assertRaisesRegex(ConfigError, r"\Ahosts: want a mapping"):
            Driver(adapters={}, hosts=1, default_host="fake")

    def test_parsed_hosts_are_taken_as_given(self):
        driver = Driver(adapters={}, hosts=Hosts("x", {"x": {}}))
        self.assertEqual(
            (driver.registry.settings.hosts, driver.registry.settings.default_host),
            ({"x": {}}, "x"),
        )

    def test_from_config_takes_hosts_parsed_already_and_a_harness_host(self):
        driver = Driver.from_config({}, hosts=Hosts("x", {"x": {}}), harness_host="x")
        self.assertEqual(
            (driver.registry.settings.hosts, driver.harness_host, driver.registry.configured()),
            ({"x": {}}, "x", []),
        )

    def test_a_default_host_goes_only_with_host_tables(self):
        with self.assertRaisesRegex(
            ConfigError,
            r"\Adefault_host: give it with \[hosts\.<name>\] tables, not parsed Hosts\Z",
        ):
            Driver(adapters={}, hosts=Hosts("x", {"x": {}}), default_host="x")

    def test_harness_host_is_kept(self):
        tables = {"adapters": {}, "hosts": HOSTS, "default_host": "tmux"}
        self.assertIsNone(Driver(**tables).harness_host)
        self.assertEqual(Driver(**tables, harness_host="tmux").harness_host, "tmux")


class StartTest(Base):
    def test_refuses_an_unknown_mode(self):
        for bad in ("pane", "", "Headless", None):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f"mode must be one of headless, tui, in_process, got {bad!r}",
                    lambda *, bad=bad: drive(self, mode=bad),
                )

    def test_refuses_a_resume_that_is_no_session_id(self):
        for bad in ("", 1, b"x" * 100):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f"resume must be a session id, got {bad!r:.80}",
                    lambda *, bad=bad: drive(self, resume=bad),
                )

    def test_refuses_a_sid_other_than_resume(self):
        self.refused(
            ConfigError,
            "sid 's1' differs from resume 'r1'",
            lambda: drive(self, sid="s1", resume="r1"),
        )


class WritableDirsTest(Base):
    def dirs(self, **kwargs):
        return mock.patch.object(ProbeAdapter, "writable_dirs", autospec=True, **kwargs)

    def test_the_spec_mapped_with_runtime_and_resume(self):
        spec = self.spec(tier=2)
        with self.dirs(return_value=[self.dir]) as dirs:
            self.assertEqual(self.driver.writable_dirs("probe", spec, "r1"), (self.dir,))
            self.assertEqual(self.driver.writable_dirs("probe", spec), (self.dir,))
        self.assertEqual(
            [(c.args[1:], c.kwargs) for c in dirs.call_args_list],
            [
                ((dataclasses.replace(spec, runtime="probe", sid="r1", resume=True),), {}),
                ((dataclasses.replace(spec, runtime="probe"),), {}),
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


class SessionStatusTest(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(self.workdir)
        RunLog(self.workdir).note(
            "session", sid="s1", cwd=self.dir, project=False, transcript="", resume=""
        )

    def test_an_unrecorded_sid_is_not_resumable_for_the_deprecated_wrapper(self):
        absent = os.path.join(self.dir, "no-record")
        for workdir in (self.workdir, absent):
            with self.subTest(workdir=workdir):
                self.assertEqual(
                    self.driver.session_status("probe", "missing", self.dir, workdir=workdir),
                    SessionStatus("no", None, ""),
                )
        self.assertFalse(os.path.exists(absent))

    def test_an_adapter_driver_error_is_not_mapped_to_missing_record(self):
        error = DriverError("inspection refused")
        with (
            mock.patch.object(ProbeAdapter, "session_status", side_effect=error),
            self.assertRaises(DriverError) as caught,
        ):
            self.driver.session_status("probe", "s1", self.dir, workdir=self.workdir)
        self.assertIs(caught.exception, error)

    def test_the_adapters_session_status(self):
        self.assertEqual(
            self.driver.session_status("probe", "s1", self.dir, workdir=self.workdir),
            SessionStatus("yes", 1.5, f"probe s1 {self.dir}"),
        )

    def test_the_public_query_resolves_cwd_from_the_required_workdir(self):
        self.assertEqual(
            self.driver.session_status("probe", "s1", self.home, workdir=self.workdir),
            SessionStatus("yes", 1.5, f"probe s1 {self.dir}"),
        )

    def test_unknown_by_default(self):
        self.assertEqual(
            self.driver.session_status("fake", "s1", "/c", workdir=self.workdir),
            SessionStatus("unknown", None, ""),
        )

    def test_an_os_error_from_the_adapter_is_unknown(self):
        with mock.patch.object(
            ProbeAdapter, "session_status", side_effect=PermissionError(13, "denied")
        ):
            self.assertEqual(
                self.driver.session_status("probe", "s1", self.dir, workdir=self.workdir),
                SessionStatus("unknown", None, ""),
            )

    def test_refuses_a_bad_sid_or_cwd_and_passes_registry_errors(self):
        for sid, shown in (("", "''"), ("a/b", "'a/b'"), (5, "5")):
            with self.subTest(sid=sid), self.assertRaises(ConfigError) as caught:
                self.driver.session_status("probe", sid, self.dir, workdir=self.workdir)
            self.assertEqual(
                str(caught.exception),
                f'sid must be a non-empty printable string without "/", got {shown}',
            )
        for cwd, shown in (("rel", "'rel'"), ("", "''"), ("/c\n", r"'/c\n'"), (None, "None")):
            with self.subTest(cwd=cwd), self.assertRaises(ConfigError) as caught:
                self.driver.session_status("probe", "s1", cwd, workdir=self.workdir)
            self.assertEqual(
                str(caught.exception), f"cwd must be a printable absolute path, got {shown}"
            )
        with self.assertRaises(ConfigError) as caught:
            self.driver.session_status("nope", "s1", self.dir, workdir=self.workdir)
        self.assertEqual(
            str(caught.exception),
            "no [adapters.nope] table in the configured adapters",
        )


class UsageTest(Base):
    def test_the_probe_of_its_session_class(self):
        probe = mock.Mock(return_value="7")
        self.driver_dependencies = {**self.driver_dependencies, "probe": probe}
        self.configure("probe", probe={"argv": ("probe-cmd",)})
        self.assertEqual(self.driver.usage("probe"), Usage(Window(7.0, None), None))
        self.assertEqual(probe.call_args.kwargs["timeout"], hosted.PROBE_TIMEOUT)


class AssignedTest(TuiBase):
    """host, probe and clock assigned to a Driver serve its later calls, as on main; sessions it
    started before keep theirs.
    """

    def test_a_probe_assigned_serves_later_usage(self):
        first, second = mock.Mock(return_value="7"), mock.Mock(return_value="9")
        self.driver_dependencies = {**self.driver_dependencies, "probe": first}
        self.configure("probe", probe={"argv": ("probe-cmd",)})
        self.assertEqual(self.driver.usage("probe"), Usage(Window(7.0, None), None))
        self.driver.probe = second
        self.assertEqual(self.driver.usage("probe"), Usage(Window(9.0, None), None))
        self.assertEqual((first.call_count, second.call_count), (1, 1))
        self.assertIs(self.driver.registry, self.driver.session_cls._config().registry)

    def test_a_clock_assigned_times_only_the_sessions_started_after(self):
        self.steps({"sleep": 0.5})
        before = drive(self, runtime="checked", mode="in_process", idle_timeout=60)
        ticks = itertools.count(0, 1000)
        self.driver.clock = lambda: float(next(ticks))
        after = drive(self, runtime="checked", mode="in_process", idle_timeout=60)
        self.assertEqual(next(after.events()), IdleTimeout(60))
        self.assertEqual(self.drain(before), [Exited(0)])


class CloseTest(TuiBase):
    def test_a_bad_name_is_refused_before_tmux_or_a_path(self):
        self.name_refused(self.driver.close)


class ShowTest(TuiBase):
    def setUp(self):
        super().setUp()
        self.host.status.return_value = tmux_host.PaneStatus.RUNNING
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
            self.assertLogs("session") as logs,
        ):
            self.assertEqual(self.driver.show("w1"), "docker exec -it box tmux attach -t '=w1'")
        self.assertEqual(
            logs.output,
            [f"WARNING:session:show w1: {NO_PANE}; watch it with tmux attach -t '=w1'"],
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
        self.host.status.return_value = tmux_host.PaneStatus.RUNNING
        self.host.opener.side_effect = tmux_host.TuiError(NO_PANE)
        self.assertIsNone(self.driver.layout("below", "m1"))
        self.assertEqual(
            self.host.mock_calls,
            [mock.call.status("m1"), mock.call.anchor("m1"), mock.call.opener()],
        )

    def test_no_pane_or_no_opener_is_a_driver_error_with_a_hint_without_split_from(self):
        hint = ": run from tmux or iTerm2, or pass split_from"
        self.host.status.return_value = tmux_host.PaneStatus.RUNNING
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
    def setUp(self):
        super().setUp()
        self.tool = os.path.join(self.dir, "tool")
        Path(self.tool).touch(mode=0o700)
        allocate = tempfile.mkdtemp

        def private(**kw):
            self.assertEqual(kw, {"prefix": "apm-group-", "dir": "/tmp"})
            return allocate(prefix=kw["prefix"], dir=self.dir)

        self.enterContext(mock.patch("tempfile.mkdtemp", side_effect=private))

    def test_the_default_host_runs_argv_with_the_signals_blocked_the_opener_its_display(self):
        def detached(_name, _argv, **kw):
            control = ProcessControl(kw["control"])
            self.addCleanup(control.remove)
            Path(control.path, "started.json").write_text('{"started": true}')
            Path(control.path, "done.json").write_text('{"rc": 0}')
            return "4242 $3 %7 4343"

        self.host.detach.side_effect = detached
        self.host.process.return_value = "4242 $3 %7 4343"
        self.host.option.return_value = None
        env = {"PATH": "/bin", "TMUX": "x"}
        self.assertIsNone(self.driver.detach("d1", [self.tool, "a b"], self.dir, env, opener="mgr"))
        control = ProcessControl(self.host.detach.call_args.kwargs["control"])
        self.assertEqual(
            json.loads(Path(control.path, "launch.json").read_text()),
            {
                "argv": [self.tool, "a b"],
                "cwd": self.dir,
                "env": env,
            },
        )
        self.assertEqual(
            self.host.mock_calls,
            [
                mock.call.validate_start("d1", (self.tool, "a b"), env, {}),
                mock.call.detach(
                    "d1",
                    engine_types.Launch(tuple(control.command()), cwd=self.dir, env=env),
                    opener="mgr",
                    display="mgr",
                    events=None,
                    record=None,
                    control=control.path,
                ),
            ],
        )
        self.driver.detach("d2", (self.tool,), self.dir, env)
        self.assertEqual(
            {k: self.host.detach.call_args.kwargs[k] for k in ("opener", "display")},
            {"opener": None, "display": None},
        )

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
            self.driver.detach("d1", [self.tool], self.dir, {})
        self.assertEqual(str(caught.exception), "detach d1: session d1 exists")
