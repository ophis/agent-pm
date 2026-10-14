"""Delegate refusal types and a start's SessionError as the run's result."""

import os
import subprocess
import sys
from unittest import mock

from agent_pm.delegate import ConfigError, Finished, Result, Spec
from agent_pm.delegate.composer import Composer
from agent_pm.delegate.engine.drivers import AgentDriver
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import SessionError

from delegate.tests.case import REPO, child_env
from delegate.tests.fakes.harness_session import HarnessSession
from delegate.tests.harness import HarnessCase
from delegate.tests.runner import executed


class PlacementSession(HarnessSession):
    @classmethod
    def host(cls, name=None):
        if name == "unknown":
            raise SessionConfigError("unknown host unknown")
        return super().host(name)

    @classmethod
    def display(cls, display=None, *, host=None):
        if display == "invalid":
            raise SessionConfigError("invalid display")
        return display


class MissingTmuxSession(HarnessSession):
    @classmethod
    def preflight(cls, spec, **placement):
        raise SessionError("tmux not found")

    @classmethod
    def start(cls, spec, **placement):
        raise SessionError("tmux not found")


class Refusals(HarnessCase):
    def construct(self, *, session_cls=HarnessSession, **placement):
        spec = Spec("input", self.work, task="echo", cwd=self.work, client="fake")
        return HarnessDelegate("dummy-tester", spec, session_cls=session_cls, **placement)

    def test_host_display_and_harness_configuration_refusals_are_delegate_errors(self):
        for placement in ({"host": "unknown"}, {"display": "invalid"}):
            with self.subTest(placement=placement), self.assertRaises(ConfigError):
                self.construct(session_cls=PlacementSession, **placement)
            self.assertFalse(os.path.exists(self.work))
        with open(self.local, "a", encoding="utf-8") as file:
            file.write('[delegate.delegates]\nharness_host = "unknown"\n')
        with self.assertRaisesRegex(ConfigError, "harness_host"):
            self.make()
        self.assertFalse(os.path.exists(self.work))

    def test_client_preflight_driver_failure_is_a_constructor_refusal_without_metadata(self):
        with self.assertRaisesRegex(ConfigError, "tmux not found"):
            self.construct(session_cls=MissingTmuxSession)
        self.assertFalse(os.path.exists(self.work))

    def test_configuration_read_failures_are_delegate_errors_in_constructor_and_attach(self):
        self.make()
        before = self.metadata()
        for error in (OSError("cannot read configuration"), ValueError("bad configuration")):
            for action in (
                self.make,
                lambda: HarnessDelegate.attach(self.work, session_cls=HarnessSession),
            ):
                with (
                    self.subTest(error=error, action=action),
                    mock.patch("builtins.open", side_effect=error),
                    self.assertRaisesRegex(ConfigError, str(error)),
                ):
                    action()
        self.assertEqual(self.metadata(), before)

    def test_a_start_driver_error_is_the_result_with_one_finished(self):
        events = []
        spec = Spec("input", self.work, task="echo", mode="tui", cwd=self.work, name="agent")
        result = executed(
            Composer().resolve("dummy-tester", "echo"),
            spec,
            driver=AgentDriver(MissingTmuxSession),
            on_event=events.append,
        )
        self.assertEqual(result, Result(1, None, "tui: tmux not found"))
        self.assertEqual(events, [Finished(result)])

    def test_prompt_rerun_after_output_failure_replaces_the_incomplete_attempt(self):
        cli = [
            sys.executable,
            "-P",
            "-m",
            "agent_pm.delegate.cli",
            "prompt",
            "--role",
            "dummy-tester",
            "--task",
            "echo",
            "--input",
            "input",
            "--workdir",
            self.work,
        ]
        environment = child_env(None, REPO)
        with open(os.path.join(self.home, "missing"), "w", encoding="utf-8") as file:
            file.write("not a directory")
        failed = subprocess.run(
            [*cli, "--out", os.path.join(self.home, "missing", "out.md")],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(failed.returncode, 0)
        output = os.path.join(self.work, "out.md")
        resumed = subprocess.run(
            [*cli, "--out", output],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue(os.path.isfile(output))
        self.assertFalse(os.path.exists(os.path.join(self.work, "delegate.json")))
