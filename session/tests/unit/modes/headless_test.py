"""modes/headless.py: its start from a Spec (checks, one session line, then the launch's command
with the child env; start failures), send and screen refused, over a fake Popen that starts no
process. Real children: session/tests/integration/modes/headless_integration_test.py.
"""

import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.engine.types import (
    Access,
    ConfigError,
    DriverError,
    Launch,
    Prepared,
    SessionInfo,
    Spec,
)

from session.tests.headless_support import NOTHING, SID, HeadlessCase, StubAdapter, StubRun


class NoHeadless(StubAdapter):
    def headless(self):
        return None


class IdentityAdapter(Adapter):
    identity_env = ("HM_ID",)

    def headless(self):
        return None

    def tui(self):
        return None


class FailingRun(StubRun):
    def launch(self, spec: Spec):
        raise LookupError("no binding")


class UnstreamedRun(StubRun):
    def events(self, lines):
        raise self.adapter.failure


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
            with self.subTest(message=message), self.assertRaises(DriverError) as caught:
                call()
            self.assertEqual(str(caught.exception), message)


class Start(Base):
    def test_one_session_line_then_the_launch_command_with_the_child_env_less_identities(self):
        launch = Launch(
            NOTHING,
            env={"HM_SET": "1"},
            unset=("HM_UNSET",),
            transcript="/t.jsonl",
            resume="stub --resume s1",
        )
        self.driver_dependencies = {"class_loader": lambda _path, _base: IdentityAdapter}
        self.configure_session(
            {"inner": {"implementation": "agent_pm.session.adapters.inner:InnerAdapter"}}
        )
        with (
            mock.patch.dict(os.environ, {"HM_UNSET": "x", "HM_ID": "i", "HM_KEEP": "k"}),
            self.faked() as calls,
        ):
            prepared = self.start(StubAdapter(launch), access=Access(project=True), prompt="hi")
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
            (NOTHING, self.dir, {**env, "HM_SET": "1", "PWD": self.dir}, 1),
        )
        self.assertEqual((kwargs["stdin"], kwargs["process_group"]), (subprocess.DEVNULL, 0))
        (spec,) = StubRun.specs
        self.assertEqual(prepared, Prepared(spec, info, NOTHING, kwargs["env"]))
        self.assertEqual((spec.prompt, spec.sid, spec.runtime), ("hi", SID, "stub"))

    def test_its_cwd_is_the_launch_cwd_else_the_access_cwd_else_the_workdir(self):
        given, access = (os.path.join(self.dir, d) for d in ("given", "access"))
        for launched, cwd, want in (
            (given, access, given),
            ("", access, access),
            ("", "", self.dir),
        ):
            with self.subTest(want=want), self.faked() as calls:
                prepared = self.start(
                    StubAdapter(Launch(NOTHING, cwd=launched)), access=Access(cwd=cwd)
                )
                self.assertEqual(
                    (prepared.info.cwd, calls[0][1]["cwd"], prepared.env["PWD"]), (want,) * 3
                )

    def test_the_runtime_note_follows_the_prompt_only_with_commands(self):
        quiet = StubAdapter()
        quiet.runtime_note = ""
        for adapter, commands, prompt in (
            (StubAdapter(), (), "hi"),
            (StubAdapter(), ("git",), "hi\n\nSTUB NOTE"),
            (quiet, ("git",), "hi"),
        ):
            with self.subTest(commands=commands, note=adapter.runtime_note), self.faked():
                prepared = self.start(adapter, access=Access(commands=commands), prompt="hi")
                self.assertEqual((prepared.spec.prompt, StubRun.specs[-1].prompt), (prompt, prompt))

    def test_refused_before_its_session_line_or_a_process(self):
        for adapter, fields, error, message in (
            (NoHeadless(), {}, ConfigError, "runtime 'stub' does not support headless mode"),
            (StubAdapter(), {"name": "w1"}, ConfigError, "name: tui mode only"),
            (StubAdapter(), {"events": "/e"}, ConfigError, "events: tui mode only"),
            (StubAdapter(), {"display": "mgr"}, ConfigError, "display: tui mode only"),
            (StubAdapter(), {"protect": ("/p",)}, ConfigError, "protect: tui mode only"),
            (
                StubAdapter(),
                {"flags": ("--verbose", "--resume", SID)},
                ConfigError,
                "--resume: picks the session; use sid= or resume=",
            ),
            (StubAdapter(), {"sid": None}, ConfigError, "sid: missing"),
            (StubAdapter(Launch(())), {}, ConfigError, "runtime 'stub' gave no command"),
            (StubAdapter(run=FailingRun), {}, LookupError, "no binding"),
        ):
            with self.subTest(message=message):
                with (
                    mock.patch("subprocess.Popen", side_effect=AssertionError("started")) as popen,
                    self.assertRaises(error) as caught,
                ):
                    self.start(adapter, **fields)
                self.assertEqual(str(caught.exception), message)
                popen.assert_not_called()
        self.assertEqual((self.recorded, self.mode), ([], None))

    def test_its_checks_in_order_tui_keys_selection_adapters_then_protect(self):
        self.configure_session(
            {"broken": {"implementation": "agent_pm.session.adapters.broken:BrokenAdapter"}}
        )
        for fields, message in (
            ({"name": "w1", "flags": ("--resume",)}, "name: tui mode only"),
            (
                {"flags": ("--resume",), "protect": ("/p",)},
                "--resume: picks the session; use sid= or resume=",
            ),
            ({"protect": ("/p",)}, "[adapters.broken].implementation: "),
            ({}, "[adapters.broken].implementation: "),
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
                    self.assertRaises(DriverError) as caught,
                ):
                    self.start()
                self.assertEqual(str(caught.exception), f"cannot start {sys.executable}: {error}")
                popen.assert_called_once()
                self.assertEqual((len(self.recorded), self.mode), (1, None))

    def test_a_stream_setup_failure_is_a_driver_error_after_the_session_line_with_no_session(self):
        for error in (OSError("no stream"), ValueError("bad stream")):
            with self.subTest(error=error):
                self.recorded, adapter = [], StubAdapter(run=UnstreamedRun)
                adapter.failure = error
                proc = mock.Mock(stdout=io.StringIO(), stderr=io.StringIO())
                with (
                    mock.patch("subprocess.Popen", return_value=proc) as popen,
                    self.assertRaises(DriverError) as caught,
                ):
                    self.start(adapter)
                self.assertEqual(str(caught.exception), f"cannot start {sys.executable}: {error}")
                self.assertIs(caught.exception.__cause__, error)
                popen.assert_called_once()
                self.assertEqual((len(self.recorded), self.mode), (1, None))


if __name__ == "__main__":
    unittest.main()
