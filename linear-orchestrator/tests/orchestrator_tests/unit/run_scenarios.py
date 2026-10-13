import io
import os
import signal
from contextlib import redirect_stderr
from unittest import mock

from agent_pm.linear_orchestrator.engine import run
from orchestrator_tests.unit.fake_api import FakeApi
from orchestrator_tests.unit.run_data import (
    DONE_RESULT,
    NOW,
    RUNTIME,
)
from orchestrator_tests.unit.run_fakes import FakeRun
from orchestrator_tests.unit.run_fixtures import (
    ENGINEER,
    ID,
    PROJECT,
    SID,
    UUID,
    Base,
)


class Case(Base):
    def setUp(self):
        super().setUp()
        self.handlers, self.order = {}, []
        self.delegate = FakeRun(self.order)
        self.api = FakeApi(self.order, lambda: self.gql)
        for p in (
            mock.patch.object(signal, "signal", lambda s, h: self.handlers.__setitem__(s, h)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def argv(
        self,
        *extra,
        assignee=ENGINEER,
        task=None,
        mode="new",
        target="Ophis/Agent-PM",
        uuid=UUID,
        runtime=RUNTIME,
    ):
        return [
            "--uuid",
            uuid,
            *(["--target", target] if target else []),
            f"--issue={ID}",
            f"--project={PROJECT}",
            f"--assignee={assignee}",
            f"--sid={SID}",
            f"--runtime={runtime}",
            *([f"--task={task}"] if task else []),
            f"--mode={mode}",
            *extra,
            "--input=Do it.\n",
        ]

    def main(self, *extra, **kw):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = run.main(
                self.argv(*extra, **kw),
                gql=self.gql,
                run=self.delegate,
                driver=self.api,
                now=lambda: NOW,
            )
        self.err = err.getvalue()
        return rc

    def done(self, *steps, result=DONE_RESULT):
        self.delegate.result, self.delegate.steps = result, steps

    def call(self):
        (found,) = self.delegate.calls
        return found

    def nothing_ran(self):
        self.assertEqual((self.gql.calls, self.delegate.calls), ([], []))
        self.assertFalse(os.path.exists(self.logs))
