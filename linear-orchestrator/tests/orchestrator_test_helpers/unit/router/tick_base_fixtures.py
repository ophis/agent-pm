import json
import os
from contextlib import redirect_stderr
from unittest import mock

from agent_pm.linear_orchestrator.engine import router
from orchestrator_test_helpers.unit.router.base_fixtures import Base
from orchestrator_test_helpers.unit.router.data import NOW
from orchestrator_test_helpers.unit.router.start_fixtures import Start
from orchestrator_test_helpers.unit.streams import Stderr


class TickBase(Base):
    def tick(
        self, fake, *argv, hour=2, live=(), start=None, again=False, now=NOW, tty=False, outer=None
    ):
        # outer: main's run and keychain; main then starts through its own outer
        self.api.live = None if live is None else list(live)
        self.api.order, self.api.usage_calls, self.api.info = [], [], []
        self.start = None if outer else start or Start()
        self.write_runs()
        err = Stderr(tty)
        with (
            redirect_stderr(err),
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
            self.log_dir(again),
        ):
            rc = router.main(
                list(argv),
                gql=fake,
                now=now,
                config_path=self.config_path,
                driver=self.api,
                hour=hour,
                start=self.start,
                **(outer or {}),
            )
            self.path = os.environ["PATH"]
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        with open(self.runs) as f:
            self.state = self.unmap(f.read())
        return rc

    def before(self):
        return self.unmap("".join(f"{line}\n" for line in self.lines))

    def last_run(self):
        entry = json.loads(self.state.splitlines()[-1])
        self.assertEqual(list(entry), ["ts", "kind", "issue", "sid", "role", "runtime"])
        return entry["kind"], entry["issue"], entry["sid"], entry["role"], entry["runtime"]

    def launched(self):
        (launch,) = self.start.launches
        return launch.issue

    def launched_runs(self):
        return [(a.issue, a.mode) for a in self.start.launches]
