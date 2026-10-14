"""CtlContract (utils/tests/contracts/ctl.py) against agent-pm-session and its workers, the same
assertions the fake-ctl Crew's tests use passes.
"""

import unittest

from agent_pm.utils import Record

from session.tests import live_tmux
from session.tests.cli_support import CliCase
from utils.tests.contracts.ctl import CtlContract


class SessionCtlContract(CtlContract, CliCase):
    command = "agent-pm-session"

    def call(self, *argv, stdin="", sid=None):
        return self.session(*argv, stdin=stdin, AGENT_PM_SID=sid, **self.scene("any"))

    def start(self, name, sid=None) -> Record:
        return self.record(CliCase.start(self, name, AGENT_PM_SID=sid))

    def end(self, name):
        self.server.tmux("kill-session", "-t", f"={name}")
        live_tmux.wait(
            lambda: self.server.tmux("has-session", "-t", f"={name}").returncode,
            what=f"{name} gone",
        )


if __name__ == "__main__":
    unittest.main()
