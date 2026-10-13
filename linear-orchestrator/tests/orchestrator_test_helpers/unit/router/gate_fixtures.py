
from agent_pm.linear_orchestrator.engine import router
from orchestrator_test_helpers.unit.router.base_fixtures import Base
from orchestrator_test_helpers.unit.router.data import NOW


class Gate(Base):
    """The usage gate's thresholds over the Driver's Usage (the old rate_limit_event rows)."""

    def gate(self):
        self.write_runs()
        return router.Gate(self.runs, NOW, False, driver=self.api)
