import threading
from unittest import mock

from agent_pm.session import SessionStatus
from orchestrator_tests import case
from orchestrator_tests.unit.fake_api import FakeApi
from orchestrator_tests.unit.sessions.data import CMD, T1, record


class Late:
    """Holds the first call until released, then sends it: a write that timed out and lands later.

    """

    def __init__(self, gql):
        self.gql, self.first = gql, True
        self.held, self.release, self.landed = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

    def __call__(self, query, **v):
        if not self.first:
            return self.gql(query, **v)
        self.first = False
        self.held.set()
        self.release.wait(30)
        try:
            return self.gql(query, **v)
        finally:
            self.landed.set()


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
