
from agent_pm.linear_orchestrator.engine import router
from orchestrator_test_helpers.unit.router.data import ago, issue
from orchestrator_test_helpers.unit.router.tick_base_fixtures import TickBase


class IssueFlag(TickBase):
    def nothing_resumable(self):
        def capped():
            for m in (390, 380, 370, 360):
                self.add("start", "TASK-1", f"s{m}", m)

        return [
            (
                "cap",
                issue("TASK-1", "In Progress", "researcher"),
                capped,
                "recover TASK-1 to=in_review reason=reached 4 attempts",
                "In Review",
                router.CAP_COMMENT,
            ),
            (
                "role",
                issue("TASK-1", "In Progress", "researcher"),
                lambda: self.resumable("TASK-1", "a", 60, "engineer"),
                "recover TASK-1 to=in_review reason=role (engineer) is not the assignee's (researcher)",
                "In Review",
                "The interrupted agent run's role (engineer) is not the assignee's (researcher); needs a look.",
            ),
            (
                "not resumable",
                issue("TASK-1", "In Progress", "researcher"),
                lambda: self.add("start", "TASK-1", "n", 5),
                "recover TASK-1 to=todo reason=not resumable sid=n",
                "Todo",
                router.INTERRUPTED,
            ),
            (
                "no sid",
                issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=5)),
                lambda: None,
                f"recover TASK-1 to=todo reason=no session updated={ago(minutes=5)}",
                "Todo",
                router.INTERRUPTED,
            ),
        ]
