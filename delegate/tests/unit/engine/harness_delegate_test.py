"""HarnessDelegate names its harness session in a Host's close failure."""

from unittest import mock

from agent_pm.delegate import Spec
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.session import DriverError

from delegate.tests.fakes.harness_session import RecordingHost
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
