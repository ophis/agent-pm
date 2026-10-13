from unittest import mock

from agent_pm.session import SessionStatus
from orchestrator_test_helpers import case
from orchestrator_test_helpers.unit.fake_api import FakeApi
from orchestrator_test_helpers.unit.sessions.data import CMD, T1, record


class Case(case.Case):
    def setUp(self):
        self.api = FakeApi()
        self.session, self.info = (
            mock.Mock(return_value=None),
            mock.Mock(return_value=SessionStatus("yes", None, CMD)),
        )
        self.api.session_record = self.session
        self.api.session_status = self.info

    def record(self, started_at=T1):
        return record(self.api, started_at)
