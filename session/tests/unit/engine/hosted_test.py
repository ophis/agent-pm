"""HostedSession: its configured classes and Setup, its class methods (start, attach, list, usage,
layout, writable_dirs, display, host) and a session's own methods, each test on a Driver's
configured class (self.session) with caller-owned dependencies.
"""

import contextlib
import dataclasses
import itertools
import json
import logging
import os
import stat
import tempfile
import threading
from unittest import mock

from agent_pm.session import Host, NotDisplayable
from agent_pm.session.adapters import common as selection
from agent_pm.session.adapters.base import Adapter
from agent_pm.session.engine import hosted
from agent_pm.session.engine import types as engine_types
from agent_pm.session.engine.hosted import POLL, HostedSession
from agent_pm.session.engine.records import RunLog
from agent_pm.session.engine.types import (
    Access,
    Activity,
    ActivityState,
    Blocked,
    ClientBound,
    ConfigError,
    DriverError,
    Exited,
    Hosts,
    IdleTimeout,
    Mode,
    Reply,
    SessionInfo,
    SessionRow,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
    Window,
)
from agent_pm.session.hosts.tmux import TmuxHost, tmux_host
from agent_pm.session.modes import tui

from session.tests import fake_adapter
from session.tests.case import HOSTS, SPAWNS, Case
from session.tests.fakes.host import FakeHost
from session.tests.hosted_support import (
    LAUNCHED,
    PYTHONPATH,
    SID,
    TMUX,
    Base,
    EnvBase,
    ProbeAdapter,
    TuiBase,
    fake_tmux,
    listed,
)
from session.tests.source_roots import PACKAGE_ROOTS

UNCONFIGURED = "session not configured: call Session.configure first"
FAKE_HOSTS = {"fake": {"implementation": "session.tests.fakes.host:FakeHost"}}


def parsed(tables, default) -> Hosts:
    return Host.parse_hosts({**tables, "default": default})


class ConfiguredTest(Case):
    def test_each_configured_class_has_its_own_setup_and_none_changes_hosted_session(self):
        first = HostedSession.configured(adapters={}, hosts=parsed(HOSTS, "tmux"))
        second = HostedSession.configured(adapters={}, hosts=parsed(FAKE_HOSTS, "fake"))
        self.assertTrue(issubclass(first, HostedSession))
        self.assertIsNot(first, second)
        self.assertIsInstance(first.host(), TmuxHost)
        self.assertIsInstance(second.host(), FakeHost)
        self.assertIsNone(HostedSession._setup)
        with self.assertRaises(ConfigError) as caught:
            HostedSession.host()
        self.assertEqual(str(caught.exception), UNCONFIGURED)

    def test_a_subclass_of_a_configured_class_starts_unconfigured(self):
        configured = HostedSession.configured(adapters={}, hosts=parsed(HOSTS, "tmux"))
        later = type("Later", (configured,), {})
        with self.assertRaises(ConfigError) as caught:
            later.host()
        self.assertEqual(str(caught.exception), UNCONFIGURED)

    def test_an_unconfigured_class_refuses_each_class_method_but_list(self):
        fresh = type("Fresh", (HostedSession,), {})
        spec = Spec(workdir=self.home, runtime="fake", mode=Mode.HEADLESS)
        for name, call in (
            ("start", lambda: fresh.start(spec)),
            ("attach", lambda: fresh.attach("w1")),
            ("usage", lambda: fresh.usage("fake")),
            ("layout", fresh.layout),
            ("writable_dirs", lambda: fresh.writable_dirs(spec)),
            ("display", fresh.display),
            ("host", fresh.host),
        ):
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                call()
            self.assertEqual(str(caught.exception), UNCONFIGURED)
        self.assertEqual(fresh.list(display=""), [])

    def test_configure_takes_parsed_hosts_only(self):
        with self.assertRaises(ConfigError) as caught:
            HostedSession.configured(adapters={}, hosts={**HOSTS, "default": "tmux"})
        self.assertEqual(str(caught.exception), "hosts: want parsed Hosts (Host.parse_hosts)")


class HostTest(Case):
    def test_the_default_host_built_once_per_class_from_its_table_less_implementation(self):
        built = []

        def factory(cls, name, settings):
            built.append((cls, name, settings))
            return cls(name, settings)

        tables = {"tmux": {**HOSTS["tmux"], "workers_per_column": 2}, **FAKE_HOSTS}
        configured = HostedSession.configured(
            adapters={}, hosts=parsed(tables, "tmux"), host_factory=factory
        )
        default = configured.host()
        self.assertIs(configured.host(), default)
        self.assertIs(configured.host("tmux"), default)
        self.assertIsInstance(configured.host("fake"), FakeHost)
        self.assertEqual(
            [(cls, name, settings) for cls, name, settings in built],
            [(TmuxHost, "tmux", {"workers_per_column": 2}), (FakeHost, "fake", {})],
        )
        self.assertEqual((default.name, default.settings), ("tmux", {"workers_per_column": 2}))
        other = HostedSession.configured(adapters={}, hosts=parsed(tables, "tmux"))
        self.assertIsNot(other.host(), default)

    def test_each_built_host_gets_its_own_settings_copy(self):
        hosts = parsed({"fake": {**FAKE_HOSTS["fake"], "sizes": [1]}}, "fake")
        first, second = (HostedSession.configured(adapters={}, hosts=hosts).host() for _ in "ab")
        first.settings["sizes"].append(2)
        self.assertEqual(second.settings, {"sizes": [1]})
        self.assertEqual(hosts.tables["fake"]["sizes"], (1,))

    def test_an_unknown_host_is_refused(self):
        configured = HostedSession.configured(adapters={}, hosts=parsed(HOSTS, "tmux"))
        for name in ("nope", 5):
            with self.subTest(name=name), self.assertRaises(ConfigError) as caught:
                configured.host(name)
            self.assertEqual(str(caught.exception), f"unknown host {name}")

    def test_an_implementation_that_is_no_host_is_refused(self):
        tables = {"x": {"implementation": "agent_pm.session.engine.types:Spec"}}
        configured = HostedSession.configured(adapters={}, hosts=parsed(tables, "x"))
        with self.assertRaises(ConfigError) as caught:
            configured.host()
        self.assertEqual(
            str(caught.exception),
            "[hosts.x].implementation: agent_pm.session.engine.types:Spec must be a concrete"
            " Host subclass",
        )

    def test_the_class_loader_is_the_setups(self):
        loaded = []

        def loader(path, base):
            loaded.append((path, base))
            return FakeHost

        configured = HostedSession.configured(
            adapters={}, hosts=parsed(HOSTS, "tmux"), class_loader=loader
        )
        self.assertIsInstance(configured.host(), FakeHost)
        self.assertEqual(loaded, [("agent_pm.session.hosts.tmux:TmuxHost", Host)])


class DisplayTest(TuiBase):
    def test_display_is_completed_by_its_host(self):
        self.host.opener.return_value = "mgr"
        self.assertEqual(self.session.display("/below"), "mgr/below")
        self.assertEqual(self.session.display(None, host="tmux"), "mgr")
        self.assertEqual(self.session.display("foreign value"), "foreign value")

    def test_a_display_not_printable_or_ending_in_a_semicolon_is_refused(self):
        for bad in ("a\tb", "a\nb", "x;"):
            for name, call in (
                ("display", lambda bad=bad: self.session.display(bad)),
                ("layout", lambda bad=bad: self.session.layout(display=bad)),
                ("start", lambda bad=bad: self.tui(name="w1", display=bad)),
            ):
                with self.subTest(bad=bad, name=name), self.assertRaises(ConfigError) as caught:
                    call()
                self.assertEqual(
                    str(caught.exception),
                    f"display {bad!r}: want printable text not ending in ;",
                )
        self.nothing_hosted()

    def test_layout_is_the_default_hosts_completed_display(self):
        self.host.opener.return_value = "mgr"
        self.assertEqual(self.session.layout(display="/below"), "mgr/below")
        self.assertEqual(self.host.mock_calls, [mock.call.anchor(None), mock.call.opener()])

    def test_layout_without_a_displayable_default_host_is_none(self):
        configured = HostedSession.configured(adapters={}, hosts=parsed(FAKE_HOSTS, "fake"))
        self.assertIsNone(configured.layout(display="/below"))

    def test_a_positional_display_is_refused(self):
        with self.assertRaises(TypeError):
            self.session.layout("/below")

    def test_a_tui_start_stores_its_display_as_given(self):
        self.tui(name="w1", display="mgr/below")
        self.assertEqual(self.host.start.call_args.kwargs["tags"]["@display"], "mgr/below")
        self.tui(name="w2")
        self.assertNotIn("@display", self.host.start.call_args.kwargs["tags"])

    def test_a_display_is_tui_only(self):
        self.refused(ConfigError, "display: tui mode only", lambda: self.start(display="mgr/below"))


class SessionTest(Base):
    def test_on_a_host_that_cannot_display_it_raises_not_displayable(self):
        table = {"scenario": self.scenario, "log": self.log}
        configured = HostedSession.configured(
            adapters={
                "fake": {
                    "implementation": "agent_pm.session.adapters.fake:InProcessFakeAdapter",
                    **table,
                }
            },
            hosts=parsed({**HOSTS, **FAKE_HOSTS}, "tmux"),
            class_loader=self.driver_dependencies["class_loader"],
        )
        session = configured.start(
            self.spec(runtime="fake", mode=Mode.IN_PROCESS, prompt="hi"), host="fake"
        )
        self.addCleanup(session.stop)
        self.assertFalse(session.displayable)
        for name, call in (
            ("show", session.show),
            ("attach_command", session.attach_command),
            ("screen", session.screen),
        ):
            with self.subTest(name=name), self.assertRaises(NotDisplayable) as caught:
                call()
            self.assertEqual(str(caught.exception), "host fake cannot display")
        self.assertEqual(self.drain(session)[-1], Exited(0))


class InProcessTest(Base):
    def setUp(self):
        super().setUp()
        self.configure(implementation="agent_pm.session.adapters.fake:InProcessFakeAdapter")

    def run_in_process(self, **kwargs) -> tuple[HostedSession, list]:
        """An in_process session started and drained, each of SPAWNS failing meanwhile and never
        called.
        """
        with contextlib.ExitStack() as stack:
            spawns = [
                stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
                for target in SPAWNS
            ]
            session = self.start(mode="in_process", **kwargs)
            found = self.drain(session)
        for spawn in spawns:
            spawn.assert_not_called()
        return session, found

    def logged(self) -> list[dict]:
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def test_a_spec_started_in_process_yields_reply_turn_end_then_exited_fr_12(self):
        self.steps(
            {"out": {"type": "message", "text": "hi"}},
            {"out": {"type": "result", "text": "done"}},
        )
        session = self.session.start(
            Spec(workdir=self.workdir, runtime="fake", mode=Mode.IN_PROCESS, prompt="hello")
        )
        self.addCleanup(session.stop)
        self.assertEqual(self.drain(session), [Reply("hi"), TurnEnd("done", 0), Exited(0)])
        self.assertEqual(self.logged()[0]["prompt"], "hello")

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
                session, found = self.run_in_process(
                    spec=self.spec(access=Access(cwd=given, project=project)), sid=SID
                )
                self.assertEqual(found, [Exited(0)])
                self.assertEqual(
                    (session.sid, session.runtime, session.mode, session.name),
                    (SID, "fake", "in_process", None),
                )
                info = SessionInfo(SID, given or self.workdir, project, "", "")
                self.assertEqual(session.info(), info)
                (line,) = self.written()
                self.assertEqual(
                    {k: v for k, v in line.items() if k != "ts"},
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
        session, found = self.run_in_process()
        self.assertEqual(found, [Reply("hi"), Stderr("warn"), TurnEnd("done", 0), Exited(0)])
        self.assertEqual(
            [(e["kind"], e.get("client_id"), e.get("text")) for e in self.written()],
            [("session", None, None), ("client_session", "n1", None), ("stderr", None, "warn")],
        )
        self.assertEqual(session.reply, "done")

    def test_the_runtime_note_follows_the_prompt_only_with_commands(self):
        for commands, prompt in (((), "hello"), (("git",), "hello\n\nFAKE NOTE")):
            with self.subTest(commands=commands):
                self.run_in_process(spec=self.spec(access=Access(commands=commands)))
                self.assertEqual(self.logged()[-1]["prompt"], prompt)

    def test_resume_continues_that_session(self):
        session, _ = self.run_in_process(sid=SID, resume=True)
        (entry,) = self.logged()
        self.assertEqual((session.sid, entry["sid"], entry["resume"]), (SID, SID, True))
        self.assertEqual(self.written()[0]["sid"], SID)

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
                "claude": {"implementation": "agent_pm.session.adapters.claude:ClaudeAdapter"},
                "codex": {"implementation": "agent_pm.session.adapters.codex:CodexAdapter"},
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


class Clock:
    """The session's clock and time.monotonic's stand-in: `now` moves only when a StubMode
    waits.
    """

    now = 0.0

    def monotonic(self) -> float:
        return self.now


class StubMode:
    """A Mode on a Clock: poll(timeout) gives the next script item (at, events, ended) once the
    clock reaches `at`, else ([], False), moving the clock to `at` or on by timeout, whichever is
    sooner; past LATE s it raises, so a session that never ends fails. returncode() gives `code`;
    every call is logged in `calls`.
    """

    LATE = 60

    def __init__(self, clock, script, code):
        self.clock, self.script, self.code, self.calls = clock, list(script), code, []
        self.failure = AssertionError("polled past the script")

    def poll(self, timeout):
        self.calls.append(("poll", timeout))
        if self.clock.now > self.LATE:
            raise self.failure
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


class EventsOverAModeTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.clock = Clock()
        self.configured = HostedSession.configured(
            adapters={}, hosts=parsed(HOSTS, "tmux"), clock=self.clock.monotonic
        )

    def session(self, *script, code=0, idle_timeout=None, **kwargs) -> HostedSession:
        self.mode = StubMode(self.clock, script, code)
        return self.configured(
            "fake",
            Mode.TUI,
            Spec(workdir=self.dir, sid="s1", name=kwargs.pop("name", None)),
            info=SessionInfo(
                sid="s1",
                cwd=self.dir,
                project=False,
                transcript="",
                resume="",
            ),
            idle_timeout=idle_timeout,
            running=self.mode,
            host=self.configured.host(),
            **{"adapter": fake_adapter.FakeAdapter({}), **kwargs},
        )

    def events(self, session) -> list:
        found = list(itertools.islice(session.events(), 50))
        self.assertLess(len(found), 50, found)
        return found

    def test_binding_and_late_metadata_preserve_completion(self):
        adapter = fake_adapter.FakeAdapter({})
        resolver = mock.Mock(side_effect=lambda info: dataclasses.replace(info, resume="resume n1"))
        adapter.session_metadata = resolver
        session = self.session((0, [ClientBound("n1")], True), adapter=adapter)
        RunLog(self.dir).note("session", sid="s1", cwd=self.dir, project=False)
        self.assertEqual(self.events(session), [Exited(0)])
        self.assertEqual(session.info().resume, "resume n1")
        self.assertEqual(RunLog(self.dir).session("s1").resume, "resume n1")
        RunLog(self.dir).note("result", outcome={"status": "done"})
        resolver.side_effect = lambda info: dataclasses.replace(info, transcript="/late")
        self.assertEqual(session.info().transcript, "/late")
        self.assertEqual(RunLog(self.dir).session("s1").transcript, "/late")
        self.assertEqual(sum(e["kind"] == "session" for e in RunLog(self.dir).read()), 1)
        previous = session.info()
        resolver.side_effect = OSError("metadata unavailable")
        self.assertEqual(session.info(), previous)

    def test_binding_record_failure_remains_fatal(self):
        adapter = fake_adapter.FakeAdapter({})
        adapter.session_metadata = mock.Mock(side_effect=lambda info: info)
        session = self.session((0, [ClientBound("n1")], True), adapter=adapter)
        with (
            mock.patch("os.open", side_effect=OSError("binding failed")),
            self.assertRaisesRegex(OSError, "binding failed"),
        ):
            self.events(session)
        self.assertIn(("stop",), self.mode.calls)
        session.info()
        adapter.session_metadata.assert_not_called()

    def test_a_client_binding_is_one_client_session_line_each_never_an_event(self):
        session = self.session(
            (0, [ClientBound("n1"), Activity("a")], False),
            (1, [ClientBound("n2")], False),
            (2, [TurnEnd("done")], True),
        )
        self.assertEqual(self.events(session), [Activity("a"), TurnEnd("done"), Exited(0)])
        lines = RunLog(self.dir).read()
        self.assertEqual(
            [(e["kind"], e.get("sid"), e.get("client_id")) for e in lines],
            [("client_session", "s1", "n1"), ("client_session", "s1", "n2")],
        )
        self.assertEqual([list(e) for e in lines], [["ts", "kind", "sid", "client_id"]] * 2)

    def test_a_client_binding_does_not_end_a_block(self):
        session = self.session(
            (0, [Blocked("ask")], False),
            (1, [ClientBound("n1")], False),
            (2, [Activity("a")], False),
            (3, [], True),
            idle_timeout=1.5,
        )
        self.assertEqual(self.events(session), [Blocked("ask"), Activity("a"), Exited(0)])

    def test_metadata_resolver_cannot_replace_session_identity_or_effective_project(self):
        adapter = fake_adapter.FakeAdapter({})
        adapter.session_metadata = lambda info: dataclasses.replace(
            info, sid="other", cwd="/other", project=True, transcript="/t"
        )
        session = self.session((0, [ClientBound("n1")], True), adapter=adapter)
        self.events(session)
        self.assertEqual(
            session.info(),
            SessionInfo(sid="s1", cwd=self.dir, project=False, transcript="/t", resume=""),
        )

    def test_reply_is_the_latest_reply_or_non_empty_turn_end_text(self):
        for events, reply in (
            ([], None),
            ([TurnEnd("")], None),
            ([Reply("hi"), TurnEnd("")], "hi"),
            ([TurnEnd("r1"), TurnEnd(""), Activity("")], "r1"),
            ([Reply("hi"), TurnEnd("done")], "done"),
        ):
            with self.subTest(events=events):
                session = self.session((0, events, True))
                self.events(session)
                self.assertEqual(session.reply, reply)

    def test_events_in_order_the_modes_exited_dropped_exited_last_in_poll_slices(self):
        session = self.session(
            (0, [Activity("a"), Exited(9)], False),
            (1, [TurnEnd("done")], False),
            (2, [Stderr("e")], True),
            code=3,
        )
        self.assertEqual(
            self.events(session),
            [Activity("a"), TurnEnd("done"), Stderr("e"), Exited(3)],
        )
        self.assertEqual(self.events(session), [])
        self.assertEqual(session.reply, "done")
        self.assertEqual({call[1] for call in self.mode.calls if call[0] == "poll"}, {POLL})
        self.assertEqual(self.mode.calls[-1], ("returncode", POLL))

    def test_each_idle_span_yields_idle_timeout(self):
        session = self.session(
            (0, [Activity()], False),
            (5, [Reply("r")], False),
            (5.5, [], True),
            idle_timeout=2,
        )
        self.assertEqual(
            self.events(session),
            [Activity(), IdleTimeout(2), IdleTimeout(2), Reply("r"), Exited(0)],
        )

    def test_after_blocked_no_idle_timeout_until_an_event_but_stderr(self):
        session = self.session(
            (0, [Blocked("ask")], False),
            (0.5, [Stderr("x")], False),
            (3, [Activity()], False),
            (4.5, [], True),
            idle_timeout=1,
        )
        self.assertEqual(
            self.events(session),
            [
                Blocked("ask"),
                Stderr("x"),
                Activity(),
                IdleTimeout(1),
                Exited(0),
            ],
        )

    def test_an_exception_from_the_mode_stops_it_then_propagates(self):
        session = self.session()
        with (
            mock.patch.object(self.mode, "poll", side_effect=RuntimeError("boom")),
            self.assertRaisesRegex(RuntimeError, "boom"),
        ):
            self.events(session)
        self.assertEqual(self.mode.calls, [("stop",)])
        self.assertEqual(self.events(session), [])

    def test_state_send_and_stop_are_the_modes_and_name_defaults_to_none(self):
        session = self.session()
        self.assertIsNone(session.name)
        self.assertEqual(session.state(), "unknown")
        session.send("hi")
        session.stop()
        self.assertEqual(self.mode.calls, [("state",), ("send", "hi"), ("stop",)])
        self.assertEqual(self.session(name="w1").name, "w1")


class UsageTest(Base):
    def probing(self, out="42.5", **launch) -> mock.Mock:
        """Runtime probe configured, its probe the Launch of `launch` (argv ("probe-cmd", "-x")
        unless given), the Setup's probe a mock giving `out`.
        """
        probe = mock.Mock(return_value=out)
        self.driver_dependencies = {**self.driver_dependencies, "probe": probe}
        self.configure("probe", probe={"argv": ("probe-cmd", "-x"), **launch})
        return probe

    def usage(self, out="42.5", **launch):
        probe = self.probing(out, **launch)
        return self.session.usage("probe"), probe

    def test_no_probe_is_none_and_runs_nothing(self):
        probe = self.probing()
        self.assertIsNone(self.session.usage("fake"))
        probe.assert_not_called()
        self.assertFalse(os.path.exists(self.probe_dir))

    def test_the_probes_stdout_as_the_adapter_parses_it(self):
        found, probe = self.usage("42.5")
        self.assertEqual(found, Usage(Window(42.5, None), None))
        probe.assert_called_once_with(
            ("probe-cmd", "-x"), cwd=self.probe_dir, env=mock.ANY, timeout=hosted.PROBE_TIMEOUT
        )

    def test_a_spawn_error_is_none(self):
        self.configure("probe", probe={"argv": ("probe-cmd",)})
        with mock.patch("subprocess.Popen", side_effect=OSError("no exec")) as popen:
            self.assertIsNone(self.session.usage("probe"))
        popen.assert_called_once()

    def test_a_timeout_is_none(self):
        found, probe = self.usage(None)
        self.assertIsNone(found)
        self.assertEqual(probe.call_args.kwargs["timeout"], hosted.PROBE_TIMEOUT)

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
            self.session.usage("nope")


class WritableDirsTest(Base):
    def dirs(self, **kwargs):
        return mock.patch.object(ProbeAdapter, "writable_dirs", autospec=True, **kwargs)

    def test_the_fake_runtime_claims_none(self):
        self.assertEqual(self.session.writable_dirs(self.spec(runtime="fake")), ())

    def test_the_adapters_dirs_as_a_tuple_for_the_spec_as_given(self):
        spec = self.spec(access=Access(dirs=(self.dir,), cwd=self.dir), tier=2, runtime="probe")
        given = (
            spec,
            dataclasses.replace(spec, sid="r1", resume=True),
            dataclasses.replace(spec, sid="s1"),
        )
        with self.dirs(return_value=[self.dir, self.workdir]) as dirs:
            for each in given:
                self.assertEqual(self.session.writable_dirs(each), (self.dir, self.workdir))
        self.assertEqual(
            [(c.args[1:], c.kwargs) for c in dirs.call_args_list], [((each,), {}) for each in given]
        )

    def test_refuses_a_spec_without_runtime_or_an_unknown_runtime(self):
        for spec, message in (
            (self.spec(), "spec.runtime: required"),
            (self.spec(runtime="nope"), "no [adapters.nope] table in the configured adapters"),
        ):
            with (
                self.subTest(message=message),
                self.dirs() as dirs,
                self.assertRaises(ConfigError) as caught,
            ):
                self.session.writable_dirs(spec)
            self.assertEqual(str(caught.exception), message)
            dirs.assert_not_called()

    def test_an_os_error_from_the_adapter_is_a_config_error(self):
        with (
            self.dirs(side_effect=PermissionError(13, "denied")),
            self.assertRaises(ConfigError) as caught,
        ):
            self.session.writable_dirs(self.spec(runtime="probe"))
        self.assertEqual(str(caught.exception), "probe's writable dirs: [Errno 13] denied")
        self.assertIsInstance(caught.exception.__cause__, PermissionError)


class ListTest(TuiBase):
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
        rows = self.session.list()
        self.assertEqual([type(r.state) for r in rows], [ActivityState] * 6)
        row = SessionRow
        self.assertEqual(
            rows,
            [
                row(
                    name="mgr",
                    sid=None,
                    runtime=None,
                    state="working",
                    pane=None,
                    opener=None,
                    shown=None,
                    last_lines=None,
                    own=True,
                ),
                row(
                    name="w1",
                    sid=SID,
                    runtime="fake",
                    state="done",
                    pane="%3",
                    opener="mgr",
                    shown="m",
                    last_lines=None,
                    own=False,
                ),
                row(
                    name="w2",
                    sid=None,
                    runtime=None,
                    state="blocked",
                    pane=None,
                    opener=None,
                    shown="",
                    last_lines=None,
                    own=False,
                ),
                row(
                    name="w3",
                    sid=None,
                    runtime=None,
                    state="working",
                    pane=None,
                    opener=None,
                    shown=None,
                    last_lines=None,
                    own=False,
                ),
                row(
                    name="w4",
                    sid=None,
                    runtime="fake",
                    state="dead",
                    pane=None,
                    opener=None,
                    shown=None,
                    last_lines="bye",
                    own=False,
                ),
                row(
                    name="w5",
                    sid=None,
                    runtime="fake9_",
                    state="working",
                    pane=None,
                    opener=None,
                    shown=None,
                    last_lines=None,
                    own=False,
                ),
            ],
        )
        self.assertEqual(self.host.mock_calls, [mock.call.listing(None), mock.call.own_session()])

    def test_display_keeps_the_sessions_stored_with_it_and_empty_none(self):
        self.host.listing.return_value = [
            listed("w1", display="mgr/below"),
            listed("w2", display="mgr"),
            listed("w3"),
        ]
        self.assertEqual([r.name for r in self.session.list(display="mgr/below")], ["w1"])
        self.assertEqual([r.name for r in self.session.list(display=None)], ["w1", "w2", "w3"])
        self.host.reset_mock()
        self.assertEqual(self.session.list(display=""), [])
        self.assertEqual(self.host.mock_calls, [])

    def test_match_is_compiled_for_the_listing_to_fullmatch(self):
        self.assertEqual(self.session.list(r"w[0-9]+"), [])
        ((pattern,),) = [c.args for c in self.host.listing.call_args_list]
        self.assertEqual(pattern.pattern, r"w[0-9]+")
        self.assertEqual(
            [n for n in ("w1", "w12", "xw1", "w1x") if pattern.fullmatch(n)], ["w1", "w12"]
        )

    def test_no_server_is_no_rows_without_reading_tmux_pane(self):
        self.assertEqual(self.session.list(), [])
        self.host.own_session.assert_not_called()

    def test_a_non_str_or_invalid_match_is_refused(self):
        for bad, message in (
            (5, "match must be a regular expression, got 5"),
            (b"w", "match must be a regular expression, got b'w'"),
            ("(", "match '(': missing ), unterminated subpattern at position 0"),
            ("[", "match '[': unterminated character set at position 0"),
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as caught:
                self.session.list(bad)
            self.assertEqual(str(caught.exception), message)
        self.nothing_hosted()

    def test_tmux_failing_or_a_bad_tmux_pane_is_a_driver_error_never_no_rows(self):
        self.host.listing.return_value = None
        with self.assertRaises(DriverError) as caught:
            self.session.list()
        self.assertEqual(str(caught.exception), "tmux sessions unknown: tmux failed")
        self.host.listing.return_value = [listed("w1")]
        self.host.own_session.side_effect = tmux_host.TuiError("bad $TMUX_PANE 'x'")
        with self.assertRaises(DriverError) as caught:
            self.session.list()
        self.assertEqual(str(caught.exception), "tmux sessions unknown: bad $TMUX_PANE 'x'")

    def test_a_row_is_frozen_with_these_fields(self):
        self.assertEqual(
            [f.name for f in dataclasses.fields(SessionRow)],
            ["name", "sid", "runtime", "state", "pane", "opener", "shown", "last_lines", "own"],
        )
        found = SessionRow(
            name="w1",
            sid=None,
            runtime=None,
            state="working",
            pane=None,
            opener=None,
            shown=None,
            last_lines=None,
            own=False,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            found.state = "done"


class TuiStartTest(TuiBase):
    def test_binds_the_adapter_session_selector_for_new_and_resumed_sessions(self):
        self.configure(
            tables={"claude": {"implementation": "agent_pm.session.adapters.claude:ClaudeAdapter"}}
        )
        for resumed, word in ((False, "--session-id"), (True, "--resume")):
            with self.subTest(resumed=resumed):
                self.tui(runtime="claude", name=f"w{resumed}", sid=SID, resume=resumed)
                argv = self.host.start.call_args.args[1]
                self.assertEqual(tuple(argv[:3]), ("claude", word, SID))

    def test_effective_project_override_reaches_info_record_and_tui_state(self):
        original = fake_adapter.FakeTui.launch
        for project in (True, False):

            def launch(run, *args, project=project, **kwargs):
                return dataclasses.replace(original(run, *args, **kwargs), project=project)

            with (
                self.subTest(project=project),
                mock.patch.object(fake_adapter.FakeTui, "launch", launch),
            ):
                session = self.tui(
                    name=f"project-{project}", spec=self.spec(access=Access(project=not project))
                )
            self.assertIs(session.info().project, project)
            self.assertIs(RunLog(self.workdir).session(session.sid).project, project)
            self.assertIs(tui.TuiMode.snapshot(session.name)["project"], project)

    def test_prepares_the_session_dir_records_the_session_then_begins_it_tagged_with_no_pane(self):
        order = []

        def status(_name):
            order.append(("status", os.path.exists(RunLog(self.workdir).path)))

        self.host.status.side_effect = status
        self.host.start.side_effect = lambda *_a, **_k: order.append(
            ("start", [e["kind"] for e in self.written()])
        )
        session = self.tui(name="w1", sid=SID, events=self.events)
        self.assertEqual(order, [("status", False), ("start", ["session"])])
        self.assertEqual(
            [c[0] for c in self.host.mock_calls],
            ["events_file", "tmux_path", "status", "start", "pane"],
        )
        self.assertEqual(
            (session.name, session.sid, session.runtime, session.mode), ("w1", SID, "fake", "tui")
        )
        self.assertEqual(
            session.info(),
            SessionInfo(sid=SID, cwd=self.workdir, project=False, transcript="", resume=""),
        )
        (call,) = self.host.start.call_args_list
        self.assertEqual(call.args[0], "w1")
        hook = tui._Store("w1").hook_argv(PACKAGE_ROOTS)
        self.assertEqual(call.args[1][-(1 + len(hook)) :], ["hello", *hook])
        self.assertEqual(call.kwargs["cwd"], self.workdir)
        self.assertEqual(
            {k: v for k, v in call.kwargs.items() if k not in ("cwd", "env")},
            {
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
                for k, v in tui.TuiMode.snapshot("w1").items()
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

    def test_state_send_stop_and_screen_are_the_tui_modes_attach_command_its_hosts(self):
        session = self.tui(name="w1")
        self.assertTrue(session.displayable)
        self.host.status.return_value = tmux_host.PaneStatus.RUNNING
        self.host.read.return_value = "pane text"
        self.assertEqual((session.state(), session.screen(5)), ("live", "pane text"))
        self.host.read.assert_called_once_with("%1", 5)
        self.assertEqual(session.attach_command(), "tmux attach -t '=w1'")
        session.send("hi")
        self.host.paste.assert_called_once_with("%1", "hi")
        self.host.keys.assert_called_once_with("%1", "Enter")
        session.stop()
        self.host.kill.assert_called_once_with("%1")
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))

    def test_send_and_screen_refuse_a_dead_pane(self):
        session = self.tui(name="w1")
        for code in (2, -15):
            self.host.status.return_value = code
            for call in (lambda: session.send("more"), session.screen):
                with self.subTest(code=code, call=call):
                    with self.assertRaises(DriverError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), "tui session w1 is not running")
        for unused in (self.host.paste, self.host.keys, self.host.read):
            unused.assert_not_called()

    def test_show_opens_it_at_the_display_given_else_its_stored_one(self):
        session = self.tui(name="w1", display="mgr/below")
        self.host.status.return_value = tmux_host.PaneStatus.RUNNING
        self.host.open_pane.return_value = None
        self.host.option.side_effect = lambda _target, key: {"@display": "mgr/below"}.get(key)
        self.assertIsNone(session.show())
        self.assertIsNone(session.show(display="other/right/m1"))
        with self.assertRaises(TypeError):
            session.show("other/right/m1")
        self.assertEqual(
            [c.kwargs for c in self.host.open_pane.call_args_list],
            [
                {
                    "split": "below",
                    "split_from": None,
                    "opener": "mgr",
                    "per_column": None,
                    "retile": None,
                },
                {
                    "split": "right",
                    "split_from": "m1",
                    "opener": "other",
                    "per_column": None,
                    "retile": None,
                },
            ],
        )

    def test_its_record_is_its_hosts(self):
        session = self.tui(name="w1")
        self.host.status.return_value = tmux_host.PaneStatus.RUNNING
        self.assertIsNone(session.record())
        self.host.option.assert_called_with("%1", "@record")

    def test_close_kills_it_by_name_and_removes_its_dir(self):
        session = self.tui(name="w1")
        self.host.reset_mock()
        session.close()
        self.assertEqual(self.host.mock_calls, [mock.call.kill("w1")])
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))

    def test_a_session_keeps_its_adapter_and_tmux_when_its_class_is_configured_again(self):
        self.configure("probe", tui=True)
        session, other = self.tui(runtime="probe", name="w1"), fake_tmux()
        self.session.configure(adapters={}, hosts=parsed(HOSTS, "tmux"), tmux=other)
        self.host.reset_mock()
        self.assertEqual(
            session.session_status(),
            SessionStatus("yes", 1.5, f"probe {session.sid} {self.workdir}"),
        )
        session.close()
        self.assertEqual(self.host.mock_calls, [mock.call.kill("w1")])
        self.assertFalse(os.path.lexists(os.path.join(self.root, "w1")))
        with self.assertRaisesRegex(ConfigError, r"\[adapters\.probe\]"):
            self.tui(runtime="probe", name="w2")
        self.assertEqual(other.mock_calls, [])


class SessionEnvTest(EnvBase):
    def test_a_tui_session_gets_it(self):
        self.tui(runtime="env", name="w1", spec=self.spec(access=self.access))
        (call,) = self.host.start.call_args_list
        self.assertEqual(call.kwargs["env"]["PYTHONPATH"], PYTHONPATH)


class AttachTest(TuiBase):
    def started(self, *hooked, start=True) -> None:
        """Tui session w1 started (sid SID; start False: already) on pane %1, then a hooks.jsonl
        record per (label, payload) in `hooked`, its @sid SID; the host's calls cleared.
        """
        if start:
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

    def test_a_session_with_its_reply_and_next_records_events(self):
        self.started(("tool", {"session_id": "n1"}), ("stop", {"last_message": "r1"}))
        session = self.session.attach("w1", idle_timeout=2)
        self.assertIsInstance(session, self.session)
        self.assertEqual(
            (session.name, session.sid, session.runtime, session.mode, session.reply),
            ("w1", SID, "fake", "tui", "r1"),
        )
        self.assertEqual(
            session.info(),
            SessionInfo(sid=SID, cwd=self.workdir, project=False, transcript="", resume=""),
        )
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
        self.assertEqual(next(session.events()), TurnEnd("r2", 0))
        self.assertEqual(session.reply, "r2")

    def test_its_reply_is_its_last_turn_ends_even_empty_its_blocked_as_live(self):
        for hooked, reply, blocked in (
            ((), None, False),
            ((("stop", {"last_message": "r1"}), ("stop", {}), ("tool", {})), "", False),
            ((("stop", {}),), "", False),
            ((("tool", {}), ("ask", {})), None, True),
            ((("ask", {"session_id": "n1"}), ("other", {"session_id": "n2"})), None, True),
            ((("ask", {}), ("other", {})), None, True),
        ):
            with self.subTest(hooked=hooked):
                self.host.status.return_value = None
                live = self.tui(name="w1", sid=SID, events=self.events)
                self.host.status.return_value = tmux_host.PaneStatus.RUNNING
                self.started(*hooked, start=False)
                if hooked:
                    events = live.events()
                    next(events)
                    while any(not isinstance(e, ClientBound) for e in live._queue):
                        next(events)
                attached = self.session.attach("w1")
                self.assertEqual((attached._blocked, attached.info()), (live._blocked, live.info()))
                self.assertEqual((attached.reply, attached._blocked), (reply, blocked))

    def test_after_attach_its_reply_follows_the_live_rule(self):
        self.started(("stop", {}))
        session = self.session.attach("w1")
        self.assertEqual(session.reply, "")
        with open(os.path.join(self.root, "w1", "hooks.jsonl"), "a") as f:
            for message in ("r1", ""):
                payload = {"last_message": message} if message else {}
                f.write(json.dumps({"ts": "", "label": "stop", "payload": payload}) + "\n")
        events = session.events()
        self.assertEqual([next(events), next(events)], [TurnEnd("r1", 0), TurnEnd("", 0)])
        self.assertEqual(session.reply, "r1")

    def test_attach_restores_its_one_adapters_fresh_tui_once(self):
        built, restored, load = [], [], self.driver_dependencies["class_loader"]

        def counted(path, base):
            found = load(path, base)
            if base is not Adapter:
                return found

            class Counted(found):
                def __init__(self, config):
                    built.append(self)
                    super().__init__(config)

                def tui(self):
                    adapter = self

                    class Restored(fake_adapter.FakeTui):
                        def restore(self, spec, state):
                            restored.append((adapter, self, spec.name))

                    return Restored(self)

            return Counted

        self.driver_dependencies = {**self.driver_dependencies, "class_loader": counted}
        self.configure(tui=True)
        self.started()
        for _ in range(2):
            built.clear()
            restored.clear()
            session = self.session.attach("w1")
            self.assertEqual(restored, [(built[0], session._running.run, "w1")])
            self.assertEqual(len(built), 1)

    def test_attach_refreshes_bound_metadata_without_starting_a_session(self):
        self.started(("tool", {"session_id": "n1"}))
        RunLog(self.workdir).note("result", outcome={"status": "done"})

        def metadata(_adapter, info):
            self.assertEqual(info.sid, SID)
            return dataclasses.replace(info, transcript="/attached", resume="resume n1")

        with mock.patch.object(fake_adapter.FakeAdapter, "session_metadata", metadata):
            session = self.session.attach("w1")
        self.assertEqual(session.info().transcript, "/attached")
        self.assertEqual(RunLog(self.workdir).session(SID).resume, "resume n1")
        self.assertEqual(sum(e["kind"] == "session" for e in self.written()), 1)

    def test_attached_while_blocked_no_idle_timeout_until_its_next_event(self):
        """On a fake clock (the session's, time.monotonic, time.sleep), a tool record comes at
        10 s.
        """
        hooks = os.path.join(self.root, "w1", "hooks.jsonl")
        now = [0.0]
        self.driver_dependencies = {**self.driver_dependencies, "clock": lambda: now[0]}
        self.configure(tui=True)
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
                self.host.option.side_effect = lambda _target, key, *, options=options: options[key]
                self.host.status.return_value, self.host.read.return_value = (
                    tmux_host.PaneStatus.RUNNING,
                    "",
                )
                now[0] = 0.0

                failure = AssertionError("no event")

                def sleep(seconds, *, failure=failure):
                    if now[0] > 60:
                        raise failure
                    now[0] += seconds
                    if now[0] - seconds < 10 <= now[0]:
                        with open(hooks, "a") as f:
                            f.write(json.dumps({"ts": "", "label": "tool", "payload": {}}) + "\n")

                with (
                    mock.patch("time.monotonic", lambda: now[0]),
                    mock.patch("time.sleep", sleep),
                ):
                    event = next(self.session.attach("w1", idle_timeout=1).events())
                self.assertEqual((event, round(now[0])), first)

    def test_a_bad_name_or_idle_timeout_is_refused_before_tmux_or_a_path(self):
        self.name_refused(self.session.attach)
        for bad in (0, -1, True, "1"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as caught:
                self.session.attach("w1", idle_timeout=bad)
            self.assertEqual(
                str(caught.exception),
                f"idle_timeout must be a positive number of seconds, got {bad!r}",
            )
        self.nothing_hosted()

    def test_an_unknown_host_is_refused_before_tmux_or_a_path(self):
        with self.assertRaises(ConfigError) as caught:
            self.session.attach("w1", host="nope")
        self.assertEqual(str(caught.exception), "unknown host nope")
        self.nothing_hosted()

    def test_no_session_dir_or_another_sid_is_no_tui_session(self):
        with self.assertRaises(DriverError) as caught:
            self.session.attach("w1")
        self.assertTrue(str(caught.exception).startswith("no tui session w1: "), caught.exception)
        self.nothing_hosted()
        self.started()
        for found in ("other", None):
            with self.subTest(found=found), self.assertRaises(DriverError) as caught:
                self.host.option.side_effect = lambda _name, _key, *, found=found: found
                self.session.attach("w1")
            self.assertEqual(str(caught.exception), "no tui session w1")

    def test_a_session_json_without_a_valid_identity_is_no_tui_session(self):
        self.started()
        path = os.path.join(self.root, "w1", "session.json")
        with open(path) as f:
            state = json.load(f)
        with open(path, "w") as f:
            json.dump({**state, "cwd": ""}, f)
        with self.assertRaises(DriverError) as caught:
            self.session.attach("w1")
        self.assertEqual(str(caught.exception), f"no tui session w1: {path}: bad cwd")
        self.host.pane.assert_not_called()

    def test_a_runtime_without_tui_or_a_registry_error_is_a_driver_error(self):
        self.started()
        self.configure(tui=False)
        for message in (
            "no tui session w1: runtime 'fake' has no tui mode",
            "no tui session w1: no [adapters.fake] table in the configured adapters",
        ):
            with self.subTest(message=message), self.assertRaises(DriverError) as caught:
                self.session.attach("w1")
            self.assertEqual(str(caught.exception), message)
            self.configure_session({})
        self.host.option.assert_not_called()


class ChecksTest(Base):
    def test_refuses_a_spec_without_runtime_or_mode(self):
        for field, call in (
            ("runtime", lambda: self.session.start(self.spec(mode=Mode.HEADLESS))),
            ("mode", lambda: self.session.start(self.spec(runtime="fake"))),
        ):
            with self.subTest(field=field):
                self.refused(ConfigError, f"spec.{field}: required to start", call)

    def test_refuses_an_idle_timeout_that_is_no_positive_number(self):
        for bad in (0, -1, 0.0, -0.5, float("nan"), True, False, "1"):
            with self.subTest(bad=bad):
                self.refused(
                    ConfigError,
                    f"idle_timeout must be a positive number of seconds, got {bad!r}",
                    lambda *, bad=bad: self.start(idle_timeout=bad),
                )

    def test_refuses_an_unknown_host(self):
        self.refused(ConfigError, "unknown host nope", lambda: self.start(host="nope"))

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

    def test_session_selection_flags_are_refused_before_launch(self):
        from agent_pm.session.adapters.claude import ClaudeAdapter
        from agent_pm.session.adapters.codex import CodexAdapter

        for runtime, adapter in (("claude", ClaudeAdapter), ("codex", CodexAdapter)):
            self.configure(
                tables={
                    runtime: {
                        "implementation": f"agent_pm.session.adapters.{runtime}:{adapter.__name__}"
                    }
                }
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
                selection.check_selection(flags, forbidden)

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
                selection.check_selection(
                    flags, CodexAdapter.forbidden_selection, CodexAdapter.valued_flags
                )
        with self.assertRaisesRegex(ConfigError, "fork: picks the session"):
            selection.check_selection(
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

    def test_a_failed_session_record_propagates_before_the_start(self):
        os.mkdir(self.workdir)
        target = os.path.join(self.dir, "elsewhere")
        os.symlink(target, RunLog(self.workdir).path)
        with (
            mock.patch("subprocess.Popen", side_effect=AssertionError("started")) as popen,
            self.assertRaises(OSError),
        ):
            self.start()
        popen.assert_not_called()
        self.assertFalse(os.path.exists(target))


class ModulesTest(Case):
    def test_constants_and_the_modes(self):
        self.assertEqual(hosted.POLL, 0.5)
        self.assertEqual((hosted.PROBE_DIR, hosted.PROBE_TIMEOUT), ("~/.agent-pm/probe", 120))
        self.assertEqual(sorted(hosted.MODES), sorted(Mode))
        self.assertEqual(
            {mode: cls.kind for mode, cls in hosted.MODES.items()}, {m: m for m in Mode}
        )
