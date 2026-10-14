"""HarnessDelegate names its harness session in a Host's close failure; preview and instruct write
no state; owner overlays shape its run.
"""

import os
from unittest import mock

from agent_pm.delegate import ConfigError, Result, Spec
from agent_pm.delegate.composer import Composer, RunParams
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.session import DriverError

from delegate.tests.fakes.harness_session import HarnessSession, RecordingHost
from delegate.tests.harness import SID, HarnessCase


class CloseTest(HarnessCase):
    def test_a_failed_harness_close_names_the_harness_session(self):
        spec = Spec("input", self.work, mode="tui", runtime="fake", sid=SID, cwd=self.work)
        record = self.make(spec=spec, host="agent", harness_host="harness").detach()
        name = self.metadata()["harness_name"]
        process = RecordingHost.processes["harness", name]
        with (
            mock.patch.object(process, "close", side_effect=DriverError("tmux: boom")),
            self.assertRaises(DriverError) as caught,
        ):
            HarnessDelegate.attach(record).close()
        self.assertEqual(str(caught.exception), f"close {name}: tmux: boom")


class PreviewTest(HarnessCase):
    def test_preview_names_the_sessions_hosts_and_callback_and_writes_nothing(self):
        for mode, agent in (("tui", f"dummy-tester-{SID[:8]}"), ("headless", None)):
            with self.subTest(mode=mode):
                spec = Spec("input", self.work, task="echo", mode=mode, runtime="fake", sid=SID)
                preview = HarnessDelegate.preview(
                    "dummy-tester",
                    spec,
                    session_cls=HarnessSession,
                    harness_host="harness",
                    callback=["writeback", "--issue", "ticket-id"],
                )
                self.assertEqual(
                    (
                        preview.agent,
                        preview.harness,
                        preview.host,
                        preview.harness_host,
                        preview.callback,
                        preview.plan.sid,
                        preview.plan.spec.mode,
                    ),
                    (
                        agent,
                        f"dummy-tester-{SID[:8]}-drive",
                        "agent",
                        "harness",
                        ("writeback", "--issue", "ticket-id"),
                        SID,
                        mode,
                    ),
                )
                self.assertTrue(preview.plan.prompt.endswith("\n# Input\n\ninput\n"))
        self.assertFalse(os.path.exists(self.work))
        self.assertEqual(os.listdir(self.agent_pm), ["config.local.toml"])
        self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))

    def test_preview_refuses_an_unknown_role_like_the_constructor(self):
        spec = Spec("input", self.work, runtime="fake", sid=SID)
        with self.assertRaisesRegex(ConfigError, "absent-role"):
            HarnessDelegate.preview("absent-role", spec, session_cls=HarnessSession)
        self.assertFalse(os.path.exists(self.work))


class InstructTest(HarnessCase):
    def test_instruct_delivers_the_composed_prompt_without_state_or_session(self):
        out = os.path.join(self.work, "prompt.md")
        result = HarnessDelegate.instruct(
            "dummy-tester", Spec("Hello.", self.work, task="echo", out=out)
        )
        self.assertEqual(result, Result(0, None, instruction=out))
        composer = Composer()
        want = composer.render(
            composer.resolve("dummy-tester", "echo"),
            RunParams(input="Hello.", out=out, workdir=self.work),
        )
        with open(out, encoding="utf-8") as file:
            self.assertEqual(file.read(), want)
        self.assertIn("**Your task**: `echo`", want)
        self.assertEqual(os.listdir(self.work), ["prompt.md"])
        self.assertEqual((HarnessSession.started, RecordingHost.launches), ([], []))


class UsageSession(HarnessSession):
    runtimes = []

    @classmethod
    def usage(cls, runtime):
        cls.runtimes.append(runtime)


class OverlayTest(HarnessCase):
    def test_usage_reads_the_runtime_the_owner_overlays_resolve(self):
        UsageSession.runtimes = []
        overlays = ({"roles": {"dummy-tester": {"runtime": "claude"}}},)
        HarnessDelegate.usage("dummy-tester", session_cls=UsageSession)
        HarnessDelegate.usage("dummy-tester", overlays=overlays, session_cls=UsageSession)
        self.assertEqual(UsageSession.runtimes, ["fake", "claude"])

    def test_owner_overlays_shape_the_persisted_run(self):
        self.make()
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "low")
        self.make(overlays=({"roles": {"dummy-tester": {"effort": "high"}}},))
        self.assertEqual(self.metadata()["plan"]["run"]["effort"], "high")
