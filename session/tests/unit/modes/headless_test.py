"""modes/headless.py: its start from a Spec (checks, one session line, then the launch's command
with the child env; start failures), send and screen refused, over a fake Popen that starts no
process. Real children: session/tests/integration/modes/headless_integration_test.py.
"""

import contextlib
import io
import os
import sys
import unittest
import uuid
from unittest import mock

from agent_pm.session import SESSION_NAME_RULE
from agent_pm.session.clients.base import Client
from agent_pm.session.engine.types import (
    Access,
    ClientHome,
    ConfigError,
    Launch,
    Liveness,
    Prepared,
    SessionError,
    SessionInfo,
    Spec,
)
from agent_pm.session.hosts.base import Piped
from agent_pm.session.modes.headless import QUEUE_MAX, HeadlessMode

from session.tests.case import PLUGIN_BIN
from session.tests.fakes.terminal import MemoryHost
from session.tests.headless_support import NOTHING, SID, HeadlessCase, StubClient, StubRun
from utils.tests.process_fixtures import DEVNULL


class NoHeadless(StubClient):
    def headless(self):
        return None


class IdentityClient(Client):
    identity_env = ("HM_ID",)

    def headless(self):
        return None

    def tui(self):
        return None


class FailingRun(StubRun):
    def launch(self, spec: Spec):
        raise LookupError("no binding")


class FailingCommitRun(StubRun):
    def commit(self):
        raise LookupError("cannot persist")


class UnstreamedRun(StubRun):
    def events(self, lines):
        raise self.client.failure


class Base(HeadlessCase):
    @contextlib.contextmanager
    def faked(self):
        """subprocess.Popen faked, starting nothing: each call's (argv, kwargs, session lines by
        then) in the list; its process's pipes empty.
        """
        calls = []

        def popen(argv, **kwargs):
            calls.append((argv, kwargs, len(self.recorded)))
            return mock.Mock(stdout=io.StringIO(), stderr=io.StringIO())

        with mock.patch("subprocess.Popen", side_effect=popen):
            yield calls


class TuiOnly(Base):
    def test_send_and_screen_raise_driver_error(self):
        with self.faked():
            self.start()
        for call, message in (
            (lambda: self.mode.send("x"), "send: tui mode only"),
            (self.mode.screen, "screen: tui mode only"),
            (lambda: self.mode.screen(5), "screen: tui mode only"),
        ):
            with self.subTest(message=message), self.assertRaises(SessionError) as caught:
                call()
            self.assertEqual(str(caught.exception), message)


class Start(Base):
    def test_a_commit_failure_leaves_no_session_line_or_process(self):
        with (
            self.faked() as calls,
            self.assertRaisesRegex(LookupError, "^cannot persist$") as caught,
        ):
            self.start(StubClient(run=FailingCommitRun))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual((self.recorded, self.mode, calls), ([], None, []))

    def test_one_session_line_then_the_launch_command_with_the_child_env_less_identities(self):
        launch = Launch(
            NOTHING,
            env={"HM_SET": "1"},
            unset=("HM_UNSET",),
            transcript="/t.jsonl",
            resume="stub --resume s1",
        )
        self.session_dependencies = {"class_loader": lambda _path, _base: IdentityClient}
        self.configure_session(
            {"inner": {"implementation": "agent_pm.session.clients.inner:InnerClient"}}
        )
        with (
            mock.patch.dict(
                os.environ,
                {
                    "HM_UNSET": "x",
                    "HM_ID": "i",
                    "HM_KEEP": "k",
                    "PATH": os.pathsep.join((PLUGIN_BIN, os.environ["PATH"])),
                },
            ),
            self.faked() as calls,
        ):
            prepared = self.start(
                StubClient(launch), access=Access(cwd=self.dir, project=True), prompt="hi"
            )
            env = {k: v for k, v in os.environ.items() if k not in ("HM_UNSET", "HM_ID")}
        info = SessionInfo(
            sid=SID,
            cwd=self.dir,
            project=True,
            transcript="/t.jsonl",
            resume="stub --resume s1",
        )
        ((argv, kwargs, lines),) = calls
        self.assertEqual(self.recorded, [info])
        self.assertEqual(
            (argv, kwargs["cwd"], kwargs["env"], lines),
            (
                NOTHING,
                self.dir,
                {
                    **env,
                    "HM_SET": "1",
                    "PWD": self.dir,
                    "AGENT_PM_AGENT_SESSION": "1",
                    "AGENT_PM_CLIENT": "stub",
                },
                1,
            ),
        )
        self.assertEqual((kwargs["stdin"], kwargs["process_group"]), (DEVNULL, 0))
        (spec,) = StubRun.specs
        self.assertEqual(prepared, Prepared(spec, info, NOTHING, kwargs["env"]))
        self.assertEqual((spec.prompt, spec.sid, spec.client), ("hi", SID, "stub"))

    def test_the_spec_client_replaces_the_ambient_and_launch_client(self):
        for launch_env in ({}, {"AGENT_PM_CLIENT": "stale"}):
            with (
                self.subTest(launch_env=launch_env),
                mock.patch.dict(os.environ, {"AGENT_PM_CLIENT": "ambient"}),
                self.faked() as calls,
            ):
                self.start(StubClient(Launch(NOTHING, env=launch_env)))
                self.assertEqual(calls[0][1]["env"]["AGENT_PM_CLIENT"], "stub")

    def test_its_cwd_is_the_launch_cwd_else_the_access_cwd(self):
        given, access = (os.path.join(self.dir, d) for d in ("given", "access"))
        for launched, cwd, want in (
            (given, access, given),
            ("", access, access),
        ):
            with self.subTest(want=want), self.faked() as calls:
                prepared = self.start(
                    StubClient(Launch(NOTHING, cwd=launched)), access=Access(cwd=cwd)
                )
                self.assertEqual(
                    (prepared.info.cwd, calls[0][1]["cwd"], prepared.env["PWD"]), (want,) * 3
                )

    def test_the_client_note_follows_the_prompt_only_with_commands(self):
        quiet = StubClient()
        quiet.client_note = ""
        for client, commands, prompt in (
            (StubClient(), (), "hi"),
            (StubClient(), ("git",), "hi\n\nSTUB NOTE"),
            (quiet, ("git",), "hi"),
        ):
            with self.subTest(commands=commands, note=client.client_note), self.faked():
                prepared = self.start(
                    client, access=Access(cwd=self.dir, commands=commands), prompt="hi"
                )
                self.assertEqual((prepared.spec.prompt, StubRun.specs[-1].prompt), (prompt, prompt))

    def test_refused_before_its_session_line_or_a_process(self):
        for client, fields, error, message in (
            (NoHeadless(), {}, ConfigError, "client 'stub' does not support headless mode"),
            (
                StubClient(),
                {"name": "w 1"},
                ConfigError,
                f"name must match {SESSION_NAME_RULE}, got 'w 1'",
            ),
            (StubClient(), {"events": "/e"}, ConfigError, "events: tui mode only"),
            (StubClient(), {"display": "mgr"}, ConfigError, "display: tui mode only"),
            (
                StubClient(),
                {"flags": ("--verbose", "--resume", SID)},
                ConfigError,
                "--resume: picks the session; use sid= or resume=",
            ),
            (StubClient(), {"sid": None}, ConfigError, "sid: missing"),
            (StubClient(Launch(())), {}, ConfigError, "client 'stub' gave no command"),
            (StubClient(run=FailingRun), {}, LookupError, "no binding"),
        ):
            with self.subTest(message=message):
                with (
                    mock.patch("subprocess.Popen", side_effect=AssertionError("started")) as popen,
                    self.assertRaises(error) as caught,
                ):
                    self.start(client, **fields)
                self.assertEqual(str(caught.exception), message)
                popen.assert_not_called()
        self.assertEqual((self.recorded, self.mode), ([], None))

    def test_its_checks_in_order_tui_keys_selection_then_clients(self):
        self.configure_session(
            {"broken": {"implementation": "agent_pm.session.clients.broken:BrokenClient"}}
        )
        for fields, message in (
            ({"events": "/e", "flags": ("--resume",)}, "events: tui mode only"),
            ({"flags": ("--resume",)}, "--resume: picks the session; use sid= or resume="),
            ({}, "[clients.broken].implementation: "),
        ):
            with (
                self.subTest(fields=fields),
                mock.patch("subprocess.Popen", side_effect=AssertionError("started")),
                self.assertRaises(ConfigError) as caught,
            ):
                self.start(**fields)
            self.assertTrue(str(caught.exception).startswith(message), caught.exception)
        self.assertEqual((self.recorded, StubRun.specs), ([], []))

    def test_a_spawn_failure_is_a_driver_error_after_the_session_line_with_no_session(self):
        for error in (OSError("no exec"), ValueError("bad argument")):
            with self.subTest(error=error):
                self.recorded = []
                with (
                    mock.patch("subprocess.Popen", side_effect=error) as popen,
                    self.assertRaises(SessionError) as caught,
                ):
                    self.start()
                self.assertEqual(str(caught.exception), f"cannot start {sys.executable}: {error}")
                popen.assert_called_once()
                self.assertEqual((len(self.recorded), self.mode), (1, None))

    def test_a_stream_setup_failure_is_a_driver_error_after_the_session_line_with_no_session(self):
        for error in (OSError("no stream"), ValueError("bad stream")):
            with self.subTest(error=error):
                self.recorded, client = [], StubClient(run=UnstreamedRun)
                client.failure = error
                proc = mock.Mock(stdout=io.StringIO(), stderr=io.StringIO())
                with (
                    mock.patch("subprocess.Popen", return_value=proc) as popen,
                    self.assertRaises(SessionError) as caught,
                ):
                    self.start(client)
                self.assertEqual(str(caught.exception), f"cannot start {sys.executable}: {error}")
                self.assertIs(caught.exception.__cause__, error)
                popen.assert_called_once()
                self.assertEqual((len(self.recorded), self.mode), (1, None))


class FakePiped(Piped):
    def __init__(self, codes=()):
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.codes, self.waits, self.stops, self.closes = list(codes), [], [], 0

    def wait(self, timeout):
        self.waits.append(timeout)
        return self.codes.pop(0) if self.codes else None

    def stop(self, *, ended):
        self.stops.append(ended)

    def close(self):
        self.closes += 1


class PipingHost(MemoryHost):
    def __init__(self, piped):
        super().__init__()
        self.piped, self.pipes = piped, []

    def pipe(self, name, launch, *, metadata=None):
        self.pipes.append((name, launch, metadata))
        return self.piped


class HomedRun(StubRun):
    def _home(self):
        return ClientHome("f", "d", "HOME_DIR", "c", (("a", "b"),))


class HomeHost(PipingHost):
    _inherit_env = False

    def _client_env(self, name, client, home, *, resume):
        return {home.env: f"/state/{name}/{home.dir}"} if home else {}

    def _resume_argv(self, name):
        return ("agent-pm-session", "resume", name)


class ThroughItsHost(Base):
    def start_on(self, host, client_class=StubRun, **fields):
        spec = Spec(workdir=self.dir, access=Access(cwd=self.dir), sid=SID, client="stub", **fields)
        self.mode = HeadlessMode.start(
            spec,
            StubClient(Launch(NOTHING, transcript="/t.jsonl"), run=client_class),
            registry=self.session_cls._config().registry,
            record=self.recorded.append,
            host=host,
        )
        return self.mode

    def test_start_goes_through_its_host_pipe_never_popen(self):
        host = PipingHost(FakePiped())
        with mock.patch("subprocess.Popen", side_effect=AssertionError("popen")):
            mode = self.start_on(host)
        ((name, launch, metadata),) = host.pipes
        prepared = mode.prepared
        self.assertEqual(
            (name, launch, metadata),
            (
                None,
                Launch(prepared.argv, env=prepared.env, cwd=self.dir, transcript="/t.jsonl"),
                None,
            ),
        )
        self.assertEqual(len(self.recorded), 1)

    def test_returncode_closes_the_piped_once_and_state_never(self):
        piped = FakePiped(codes=[None, None, 0, 0])
        mode = self.start_on(PipingHost(piped))
        self.assertEqual(mode.state(), Liveness.LIVE)
        self.assertIsNone(mode.returncode(1))
        self.assertEqual(piped.closes, 0)
        self.assertEqual(mode.returncode(1), 0)
        self.assertEqual(mode.returncode(1), 0)
        self.assertEqual((piped.closes, piped.waits), (1, [0, 1, 1, 1]))
        mode.stop()
        self.assertEqual(piped.stops, [True])

    def test_a_named_headless_spec_gets_the_hosts_client_env_and_metadata(self):
        host = HomeHost(FakePiped())
        with (
            mock.patch.object(uuid, "uuid4", return_value=uuid.UUID(int=7)),
            mock.patch.dict(os.environ, {"HM_AMBIENT": "1"}),
        ):
            mode = self.start_on(host, name="w1", resume=True, client_class=HomedRun)
        ((name, launch, metadata),) = host.pipes
        self.assertEqual((name, StubRun.specs[-1].env), ("w1", {"HOME_DIR": "/state/w1/d"}))
        self.assertNotIn("HM_AMBIENT", launch.env)
        self.assertEqual(
            dict(metadata),
            {
                "sid": SID,
                "client": "stub",
                "generation": uuid.UUID(int=7).hex,
                "home_field": "f",
                "home_dir": "d",
                "credential": "c",
                "home_files": '{"a": "b"}',
                "resume": "1",
            },
        )
        resume = f"cd {self.dir} && agent-pm-session resume w1"
        self.assertEqual((mode.prepared.info.resume, self.recorded[0].resume), (resume, resume))

    def test_a_nameless_spec_gets_no_host_env_metadata_or_resume(self):
        host = HomeHost(FakePiped())
        self.start_on(host, client_class=HomedRun)
        ((name, launch, metadata),) = host.pipes
        self.assertEqual((name, metadata, self.recorded[0].resume), (None, None, ""))
        self.assertEqual(StubRun.specs[-1].env, {})

    def test_preflight_validates_a_named_run_on_its_host(self):
        for name, validated in ((None, []), ("w1", ["w1"])):
            with self.subTest(name=name):
                host = HomeHost(FakePiped())
                spec = Spec(
                    workdir=self.dir, access=Access(cwd=self.dir), sid=SID, client="stub", name=name
                )
                HeadlessMode.preflight(
                    spec,
                    StubClient(Launch(NOTHING), run=HomedRun),
                    registry=self.session_cls._config().registry,
                    host=host,
                )
                self.assertEqual([c.args[0] for c in host.calls.validate.call_args_list], validated)

    def test_the_event_queue_is_bounded(self):
        mode = self.start_on(PipingHost(FakePiped()))
        self.assertEqual(mode.items.maxsize, QUEUE_MAX)


if __name__ == "__main__":
    unittest.main()
