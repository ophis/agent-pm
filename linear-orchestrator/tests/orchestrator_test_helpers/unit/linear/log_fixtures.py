
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.logs_fixtures import Logs


class Log(Logs):
    def log(self, *args, tty=False, fail=None, **kw):
        return self.stderr(lambda: linear.log(*args, **kw), tty, fail)[1]
