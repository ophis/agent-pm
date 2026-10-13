from agent_pm.linear_orchestrator.linear import sessions
from agent_pm.session import ConfigError, SessionStatus
from orchestrator_test_helpers.unit.sessions.data import RUNTIME, SID, WORKDIR
from orchestrator_test_helpers.unit.sessions.status_fixtures import Status as StatusBase


class Status1(StatusBase):
    def test_the_drivers_status_at_the_records_cwd_else_the_workdir(self):
        st = SessionStatus("no", 5.0, "")
        self.info.return_value = st
        for entry, cwd in ((None, WORKDIR), ({"kind": "session", "cwd": "/data/r"}, "/data/r")):
            with self.subTest(entry=entry):
                self.session.return_value = entry
                self.info.reset_mock()
                self.assertIs(sessions.status(WORKDIR, "rt", SID, driver=self.api), st)
                self.session.assert_called_with(WORKDIR, SID)
                self.info.assert_called_once_with("rt", SID, cwd)

    def test_errors_propagate(self):
        for target in (self.session, self.info):
            with self.subTest(target=target):
                self.session.side_effect = self.info.side_effect = None
                target.side_effect = ConfigError("bad")
                with self.assertRaises(ConfigError):
                    sessions.status(WORKDIR, RUNTIME, SID, driver=self.api)
