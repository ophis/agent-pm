from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.logs_fixtures import Logs


class ApiErrors(Logs):
    def error(self, e, tty=False, dry=False, src="router"):
        return self.stderr(lambda: linear.linear_error(src, e, dry), tty)
