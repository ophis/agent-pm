"""The real HarnessDelegate runs the Delegate contract through process-free owned Sessions."""

from agent_pm.delegate import Spec
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.session import Liveness, Mode, NotDisplayable
from agent_pm.session import Spec as SessionSpec

from delegate.tests.contracts.delegate import DelegateContract
from delegate.tests.fakes.harness_session import HarnessSession, RecordingHost, ShowingHost
from delegate.tests.integration.harness_integration_test import SID, HarnessCase


class HarnessContract(DelegateContract, HarnessCase):
    delegate_cls = HarnessDelegate
    ROLE = "dummy-tester"
    DISPLAYABLE = False

    def make(self, workdir, **kw):
        role = kw.pop("role", self.ROLE)
        spec = Spec("Hello.", workdir, runtime="fake", mode="tui", cwd=workdir, **kw)
        return HarnessDelegate(
            role, spec, session_cls=HarnessSession, host="agent", harness_host="harness"
        )

    def end(self, record):
        self.stop(record)

    def finish(self, record):
        view = HarnessDelegate.attach(record)
        view.close()
        view.run()
        view.close()


class ShowingHarnessContract(HarnessContract):
    DISPLAYABLE = True

    def setUp(self):
        super().setUp()
        script = HarnessSession.script
        HarnessSession.reset(displayable=True)
        HarnessSession.script = script


class Ownership(HarnessCase):
    def pair(self, *, agent_displayable=True, harness_displayable=True):
        HarnessSession.reset()
        for name, capable in (("agent", agent_displayable), ("harness", harness_displayable)):
            kind = ShowingHost if capable else RecordingHost
            HarnessSession.hosts[name] = kind(name)
        delegate = self.make(
            spec=Spec(
                "input",
                self.work,
                mode="tui",
                runtime="fake",
                sid=SID,
                name="agent-window",
                cwd=self.work,
            ),
            host="agent",
            harness_host="harness",
            display="target",
        )
        record = delegate.detach()
        HarnessSession.start(
            SessionSpec(
                workdir=self.work,
                runtime="fake",
                mode=Mode.TUI,
                sid=SID,
                name=self.metadata()["agent_name"],
            ),
            host="agent",
            display="target",
        )
        return record, HarnessDelegate.attach(record)

    def test_harness_owns_liveness_while_agent_owns_state_and_display_target(self):
        record, view = self.pair()
        agent = HarnessSession.live[self.metadata()["agent_name"]]
        self.assertTrue(view.alive())
        self.assertEqual(view.status().state, agent.state())
        self.assertEqual(view.attach_command(), agent.attach_command())
        self.assertEqual(view.status().attach, agent.attach_command())
        harness = RecordingHost.processes["harness", self.metadata()["harness_name"]]
        harness.close()
        self.assertFalse(view.alive())
        self.assertEqual(view.status().state, Liveness.LIVE)
        self.assertEqual(view.attach_command(), agent.attach_command())
        self.assertEqual(record.sid, SID)

    def test_dead_agent_falls_back_to_harness_and_close_closes_all_twice(self):
        _, view = self.pair()
        agent_name = self.metadata()["agent_name"]
        harness_name = self.metadata()["harness_name"]
        HarnessSession.live[agent_name].close()
        self.assertEqual(view.attach_command(), f"harness attach {harness_name}")
        view.close()
        view.close()
        self.assertFalse(view.alive())
        self.assertFalse(RecordingHost.processes["agent", agent_name].alive())
        self.assertFalse(RecordingHost.processes["harness", harness_name].alive())

    def test_show_reaches_all_live_capable_sessions(self):
        _, view = self.pair()
        view.show()
        self.assertEqual(
            set(ShowingHost.shown),
            {
                (self.metadata()["agent_name"], "target"),
                (self.metadata()["harness_name"], "target"),
            },
        )

    def test_hidden_agent_uses_harness_target_and_hidden_pair_has_no_attach(self):
        _, view = self.pair(agent_displayable=False)
        self.assertEqual(view.attach_command(), f"harness attach {self.metadata()['harness_name']}")
        view.close()
        self.work += "-hidden"
        _, view = self.pair(agent_displayable=False, harness_displayable=False)
        self.assertIsNone(view.status().attach)
        self.assertIsNone(view.attach_command())
        with self.assertRaises(NotDisplayable):
            view.show()

    def test_resume_closes_leftover_agent_before_restarting_lifecycle_owner(self):
        record, view = self.pair()
        agent_name = self.metadata()["agent_name"]
        RecordingHost.processes["harness", self.metadata()["harness_name"]].close()
        before = len(RecordingHost.launches)
        resumed = view.resume()
        self.assertEqual((resumed.sid, resumed.sessions), (record.sid, record.sessions))
        self.assertFalse(RecordingHost.processes["agent", agent_name].alive())
        self.assertEqual(len(RecordingHost.launches), before + 1)
        self.assertEqual(view.resume(), resumed)
        self.assertEqual(len(RecordingHost.launches), before + 1)
