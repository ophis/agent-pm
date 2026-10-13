import functools
import io
import os
from contextlib import redirect_stderr
from dataclasses import replace

from agent_pm.linear_orchestrator.engine import config, router
from orchestrator_test_helpers.case import PYTHONPATH
from orchestrator_test_helpers.unit.router.data import ENG_RUN, OUTER, outer_args
from orchestrator_test_helpers.unit.router.tick_base_fixtures import TickBase
from orchestrator_test_helpers.unit.run_fixtures import (
    ENGINEER,
    HARNESS,
    ID,
    PROJECT,
    SID,
    USER_NOTE,
    Gql,
    node,
)
from orchestrator_test_helpers.unit.target_support import FakeRun


class OuterBase(TickBase):
    def setUp(self):
        super().setUp()
        self.write_config(OUTER)
        self.gql = Gql(issue=node(comments=[USER_NOTE]))
        self.run_ = FakeRun(ENG_RUN)
        self.missing, self.keychain_calls = set(), []

    def keychain(self, service):
        self.keychain_calls.append(service)
        return service not in self.missing

    def outer(self, assignee=ENGINEER, task=None, mode="new", ident=ID, sid=SID, runtime="a"):
        a = outer_args(ident, PROJECT, assignee, sid, task, mode, runtime)
        cfg = replace(config.load(path=self.config_path), driver=self.api)
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(again=True):
            rc = router.outer(
                a,
                cfg,
                gql=functools.partial(self.gql, service=HARNESS),
                run=self.run_,
                keychain=self.keychain,
            )
        self.err = err.getvalue()
        return rc

    def detached(self, role="engineer"):
        name, argv, cwd, env = self.api.detached[-1]
        self.assertEqual((name, cwd), (f"agent-pm-{role}-{ID}", self.rd))
        expected = {**os.environ, "PATH": config.path(), "PYTHONPATH": os.pathsep.join(dict.fromkeys(os.path.abspath(p) for p in [*PYTHONPATH.split(os.pathsep), *os.environ.get("PYTHONPATH", "").split(os.pathsep)] if p))}
        self.assertEqual(set(env), set(expected))
        for key, value in expected.items():
            self.assertTrue(env[key] == value, key)
        return argv

    def input(self, **kw):
        last = self.detached(**kw)[-1]
        self.assertTrue(last.startswith("--input="), last[:40])
        return last.removeprefix("--input=")
