"""runtimes/headless.py: its start from a Spec (checks, one session line, then the launch's command
with the child env; start failures), send and screen refused, over a fake Popen that starts no
process. Real children: session/tests/integration/runtimes/headless_integration_test.py.
"""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from agent_pm.session.clients.base import Client
from agent_pm.session.engine.types import (
    Access,
    ConfigError,
    Launch,
    Prepared,
    SessionError,
    SessionInfo,
    Spec,
)

from session.tests.case import PLUGIN_BIN
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
                    "AGENT_PM_RUN_CLIENT": "stub",
                },
                1,
            ),
        )
        self.assertEqual((kwargs["stdin"], kwargs["process_group"]), (DEVNULL, 0))
        (spec,) = StubRun.specs
        self.assertEqual(prepared, Prepared(spec, info, NOTHING, kwargs["env"]))
        self.assertEqual((spec.prompt, spec.sid, spec.client), ("hi", SID, "stub"))

    def test_the_spec_client_replaces_the_ambient_and_launch_client(self):
        for launch_env in ({}, {"AGENT_PM_RUN_CLIENT": "stale"}):
            with (
                self.subTest(launch_env=launch_env),
                mock.patch.dict(os.environ, {"AGENT_PM_RUN_CLIENT": "ambient"}),
                self.faked() as calls,
            ):
                self.start(StubClient(Launch(NOTHING, env=launch_env)))
                self.assertEqual(calls[0][1]["env"]["AGENT_PM_RUN_CLIENT"], "stub")

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
            (StubClient(), {"name": "w1"}, ConfigError, "name: tui mode only"),
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
            ({"name": "w1", "flags": ("--resume",)}, "name: tui mode only"),
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


if __name__ == "__main__":
    unittest.main()
