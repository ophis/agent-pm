from agent_pm.linear_orchestrator.engine import router
from orchestrator_test_helpers.unit.router.data import SESSIONS
from orchestrator_test_helpers.unit.router.live_sessions_fixtures import (
    LiveSessions as LiveSessionsBase,
)


class LiveSessions1(LiveSessionsBase):
    def test_exact_names_of_the_roles(self):
        self.api.live = [
            "agent-pm-engineer-TASK-2",
            "agent-pm-engineer-TASK-10",
            "agent-pm-researcher-TASK-1",
            "agent-pm-pm-TASK-6",
            "agent-pm-pm-lead-TASK-4",
            "agent-pm-engineer",
            "agent-pm-engineer-TASK-1x",
            "agent-pm-engineer-task-3",
            "agent-pm-engineer-TASK-",
            "agent-pm-ghost-TASK-5",
            "engineer-TASK-7-0b6f2c1e",
            "other",
        ]
        self.assertEqual(
            router.live_sessions(["researcher", "pm", "pm-lead", "engineer"], driver=self.api),
            {
                "researcher": ["TASK-1"],
                "pm": ["TASK-6"],
                "pm-lead": ["TASK-4"],
                "engineer": ["TASK-10", "TASK-2"],
            },
        )
        self.assertEqual(self.api.order, SESSIONS)

    def test_unknown_sessions_are_none(self):
        self.api.live = None
        self.assertIsNone(router.live_sessions(["researcher", "engineer"], driver=self.api))
