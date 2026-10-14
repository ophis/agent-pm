"""LinearRun reattaches the real HarnessDelegate's durable state through process-free Sessions."""

from dataclasses import replace

from agent_pm.delegate import Delegate
from agent_pm.linear_orchestrator.engine import runs
from agent_pm.linear_orchestrator.engine.linear_run import LinearRun
from agent_pm.session import Mode
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import load_class
from orchestrator_tests.linear_run_case import RunCase

from delegate.tests.fakes.harness_session import HarnessSession, RecordingHost
from delegate.tests.harness import SID, HarnessCase

HARNESS_DELEGATE = load_class("agent_pm.delegate.engine.harness_delegate:HarnessDelegate", Delegate)


class HarnessProvider(HARNESS_DELEGATE):
    def __init__(self, role, spec, **options):
        super().__init__(
            role, spec, session_cls=HarnessSession, host="agent", harness_host="harness", **options
        )


class HarnessRun(HarnessCase, RunCase):
    def setUp(self):
        HarnessCase.setUp(self)
        RunCase.setUp(self)
        self.cfg = replace(
            self.cfg, delegate_kind="harness", delegates={"harness": HARNESS_DELEGATE}, protect=()
        )

    def test_real_harness_state_round_trip_owns_both_sessions_and_resumes_once(self):
        issue = "TASK-1"
        self.work = self.cfg.run_dir(issue)
        owner = LinearRun(
            issue,
            "Echo input.",
            self.work,
            cfg=self.cfg,
            role="dummy-tester",
            sid=SID,
            task="echo",
            runtime="fake",
            mode="tui",
            delegate_cls=HarnessProvider,
        )
        runs.note(
            self.cfg.runs_log,
            "start",
            issue=issue,
            sid=SID,
            role="dummy-tester",
            runtime="fake",
            delegate_kind="harness",
        )
        record = owner.detach()
        HarnessSession.start(
            SessionSpec(
                workdir=self.work,
                runtime="fake",
                mode=Mode.TUI,
                sid=SID,
                name=self.metadata()["agent_name"],
            ),
            host="agent",
        )
        view = LinearRun.attach(issue, cfg=self.cfg)
        self.assertIs(type(view.delegate), HARNESS_DELEGATE)
        self.assertTrue(view.alive())
        self.assertEqual(view.status().name, issue)
        self.assertEqual(view.resume(), record)
        self.assertEqual(len(RecordingHost.launches), 2)
        view.close()
        self.assertFalse(view.alive())
        self.assertTrue(all(not process.alive() for process in RecordingHost.processes.values()))
        resumed = view.resume()
        self.assertEqual((resumed.sid, resumed.sessions), (record.sid, record.sessions))
        self.assertEqual(view.resume(), resumed)
        self.assertEqual(len(RecordingHost.launches), 3)
        self.addCleanup(view.close)
